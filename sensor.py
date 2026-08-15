"""Sensor platform — departure sensor and ticket price sensor per monitored line."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now

from .api import VtjpAdapter
from .coordinator import VasttrafikDepartureCoordinator
from ._helpers import parse_dt, to_float  # noqa: F401 (to_float re-exported for device_tracker)
from .const import (
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_DIRECTION_GID,
    CONF_END_STOP_GID,
    CONF_END_STOP_NAME,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_NAME,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_MODE,
    DEFAULT_DELAY,
    DEPARTURE_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = DEPARTURE_SCAN_INTERVAL

# ── Transport-mode icon map ────────────────────────────────────────────────────
_MODE_ICON: dict[str, str] = {
    "bus":   "mdi:bus-clock",
    "tram":  "mdi:tram",
    "train": "mdi:train",
    "ferry": "mdi:ferry",
    "taxi":  "mdi:taxi",
}
_MODE_LABEL: dict[str, str] = {
    "bus":   "Bus",
    "tram":  "Tram",
    "train":  "Train",
    "ferry": "Ferry",
    "taxi":  "Taxi",
}

# ── Shared helpers ─────────────────────────────────────────────────────────────

def dir_key_for_line(ml: dict) -> str:
    """
    Stable direction discriminator for a monitored line.

    Without this, two monitored lines that share the same stop + line but head in
    opposite directions collapse onto the same device / unique_id.

    Preference: direction GID → destination stop GID → a slug of the direction
    text → "any". The direction-text fallback matters because the v4 departures
    model has no destinationStopArea field, so CONF_DIRECTION_GID is in practice
    never stored — a direction-only setup (chosen via the options "pick direction"
    step) would otherwise fall through to "any" for both directions and collide.
    """
    direction_slug = (ml.get(CONF_DIRECTION) or "").strip().lower().replace(" ", "_")
    return (
        ml.get(CONF_DIRECTION_GID)
        or ml.get(CONF_END_STOP_GID)
        or direction_slug
        or "any"
    )


def device_info_for_line(entry_id: str, ml: dict) -> DeviceInfo:
    """
    Build a DeviceInfo that is identical across all three entity platforms
    for the same monitored-line entry.  Passing the same identifiers groups
    sensor + binary_sensor + device_tracker under one device card in HA.

    The direction discriminator is part of the identifier so the same line at the
    same stop in two directions produces two distinct devices (not one merged card).
    """
    stop_gid  = ml.get(CONF_STOP_GID, "")
    line_name = ml.get(CONF_LINE_NAME, "")
    mode      = (ml.get(CONF_TRANSPORT_MODE) or "bus").lower()
    stop_name = ml.get(CONF_STOP_NAME, "")

    device_name = ml.get(CONF_NAME) or f"Linje {line_name} – {stop_name}"
    if ml.get(CONF_END_STOP_NAME):
        device_name = ml.get(CONF_NAME) or (
            f"Linje {line_name} – {stop_name} → {ml[CONF_END_STOP_NAME]}"
        )

    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{stop_gid}_{line_name}_{dir_key_for_line(ml)}")},
        name=device_name,
        manufacturer="Västtrafik",
        model=_MODE_LABEL.get(mode, "Transit"),
        entry_type=DeviceEntryType.SERVICE,
    )


def _best_departure_dt(dep: dict) -> datetime | None:
    """
    Return the best available departure time for display.

    REST.md section 6.2.1 says to use estimatedOtherwisePlannedTime as the
    recommended "best time" field — it returns estimated if realtime data exists,
    otherwise planned. This avoids the need to check both fields separately.

    OAS DepartureApiModel fields (in preference order):
      estimatedOtherwisePlannedTime  ← use this for display (API's own recommendation)
      estimatedTime                  ← realtime only
      plannedTime                    ← fallback
    """
    for key in (
        "estimatedOtherwisePlannedTime",
        "estimatedTime",
        "plannedTime",
    ):
        dt = parse_dt(dep.get(key))
        if dt:
            return dt
    return None


def _planned_departure_dt(dep: dict) -> datetime | None:
    """Return planned departure time only — used to calculate delay."""
    return parse_dt(dep.get("plannedTime"))


# Human labels for the boolean service flags on DirectionDetailsApiModel.
_DIRECTION_FLAG_LABELS: dict[str, str] = {
    "isExtraBus":            "extra_bus",
    "isExtraBoat":           "extra_boat",
    "isExtraTram":           "extra_tram",
    "isExpressBus":          "express_bus",
    "isSchoolBus":           "school_bus",
    "isDirectDestinationBus": "direct_bus",
    "isSwimmingService":     "swimming_service",
    "isFrontEntry":          "front_entry",
    "isFreeService":         "free_service",
    "isPaidService":         "paid_service",
}


def _direction_extras(sj: dict) -> dict[str, Any]:
    """
    Pull the useful bits out of serviceJourney.directionDetails (v4 Swagger).

    Returns: via, short_direction, replaces_line, fortifies_line, and a
    service_flags list containing only the flags that are set to true.
    """
    dd = sj.get("directionDetails") or {}
    flags = [label for key, label in _DIRECTION_FLAG_LABELS.items() if dd.get(key)]
    return {
        "via":             dd.get("via"),
        "short_direction": dd.get("shortDirection"),
        "replaces_line":   dd.get("replaces"),
        "fortifies_line":  dd.get("fortifiesLine"),
        "service_flags":   flags,
    }


# ── Platform setup ─────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    api: VtjpAdapter = store["api"]
    coordinators: list[VasttrafikDepartureCoordinator] = store["coordinators"]
    entities: list[SensorEntity] = []

    for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, [])):
        coordinator = coordinators[i]
        entities.append(VasttrafikDepartureSensor(coordinator, ml, entry.entry_id, i))
        # Ticket sensor — only when an end stop is configured
        if ml.get(CONF_END_STOP_GID) and ml.get(CONF_STOP_GID):
            entities.append(VasttrafikTicketSensor(hass, api, ml, entry.entry_id, i))

    if entities:
        # Departure sensors (CoordinatorEntity) compute state in __init__; the ticket
        # sensor still needs an initial poll — update_before_add is a no-op for the former.
        async_add_entities(entities, update_before_add=True)


# ── Sensor entity ──────────────────────────────────────────────────────────────

class VasttrafikDepartureSensor(CoordinatorEntity, SensorEntity):
    """
    Departure sensor.

    State   : next departure as a timezone-aware datetime
              → HA renders this as 'in 4 min' / '14:32' in the UI automatically
    Attrs   : platform, delay, is_realtime, upcoming (next 3), service_journey_gid
    Device  : shared with the Störning binary_sensor and Position device_tracker
    Data    : the shared VasttrafikDepartureCoordinator (also feeds the tracker).
    """

    _attr_has_entity_name  = True
    _attr_name             = None          # primary entity → uses device name
    _attr_device_class     = SensorDeviceClass.TIMESTAMP
    _attr_attribution      = "Data provided by Västtrafik"

    def __init__(
        self,
        coordinator: VasttrafikDepartureCoordinator,
        ml: dict,
        entry_id: str,
        idx: int,
    ) -> None:
        super().__init__(coordinator)
        self._ml    = ml

        stop_gid    = ml.get(CONF_STOP_GID, "")
        line_name   = ml.get(CONF_LINE_NAME, "")
        dir_key     = dir_key_for_line(ml)

        # Content-based unique ID — survives re-ordering of lines in config
        self._attr_unique_id  = f"{entry_id}_dep_{stop_gid}_{line_name}_{dir_key}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        mode = (ml.get(CONF_TRANSPORT_MODE) or "bus").lower()
        self._attr_icon = _MODE_ICON.get(mode, "mdi:bus-clock")

        self._delay             = timedelta(minutes=ml.get(CONF_DELAY, DEFAULT_DELAY))
        self._departure_dt: datetime | None = None
        self._extra: dict[str, Any] = {}
        # Coordinator data is already available (refreshed at setup) — compute now.
        self._process()

    # ── SensorEntity ──────────────────────────────────────────────────────────

    @property
    def native_value(self) -> datetime | None:
        """Next departure as a timezone-aware datetime for HA's relative-time rendering."""
        return self._departure_dt

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "line":           self._ml.get(CONF_LINE_NAME),
            "stop":           self._ml.get(CONF_STOP_NAME),
            "direction":      self._ml.get(CONF_DIRECTION) or "any",
            "end_stop":       self._ml.get(CONF_END_STOP_NAME),
            "walk_minutes":   int(self._delay.total_seconds() // 60),
            **self._extra,
        }

    # ── Coordinator-driven update ───────────────────────────────────────────────

    @callback
    def _handle_coordinator_update(self) -> None:
        self._process()
        self.async_write_ha_state()

    def _process(self) -> None:
        """Recompute state + attributes from the shared coordinator's departures."""
        data          = self.coordinator.data or {}
        departures    = data.get("departures") or []
        next_arrival  = data.get("next_arrival")
        line_name     = self._ml.get(CONF_LINE_NAME, "")
        direction_str = self._ml.get(CONF_DIRECTION) or None
        target        = now() + self._delay

        # line + direction, not cancelled, at least `delay` minutes out, earliest first.
        # The coordinator's window includes just-departed trips (for the tracker), so
        # the `>= target` filter and sort are what make "next departure" correct here.
        dir_lower = direction_str.lower() if direction_str else None
        candidates: list[tuple[datetime, dict]] = []
        for dep in departures:
            if dep.get("isCancelled"):
                continue
            sj   = dep.get("serviceJourney") or {}
            line = sj.get("line") or {}
            if (line.get("shortName") or "") != line_name:
                continue
            if dir_lower:
                d = (sj.get("direction") or "").lower()
                if d and dir_lower not in d:
                    continue
            t = _best_departure_dt(dep)
            if t is None or t < target:
                continue
            candidates.append((t, dep))

        candidates.sort(key=lambda x: x[0])
        relevant = [dep for _, dep in candidates]

        if not relevant:
            self._departure_dt = None
            self._extra = {}
            return

        first   = relevant[0]
        dep_dt  = _best_departure_dt(first)
        plan_dt = _planned_departure_dt(first)
        sj      = first.get("serviceJourney") or {}
        line    = sj.get("line") or {}
        mode    = (line.get("transportMode") or "bus").upper()

        # Delay vs planned
        delay_min: int | None = None
        if dep_dt and plan_dt:
            delay_min = max(0, int((dep_dt - plan_dt).total_seconds() // 60))

        # Dynamic icon from actual API transport mode
        self._attr_icon = {
            "BUS": "mdi:bus-clock", "TRAM": "mdi:tram",
            "TRAIN": "mdi:train",   "FERRY": "mdi:ferry", "TAXI": "mdi:taxi",
        }.get(mode, "mdi:bus-clock")

        # Platform: realtimeStopPoint is populated when stop is moved (REST.md 6.2.2)
        rt_sp      = first.get("realtimeStopPoint") or first.get("stopPoint") or {}
        orig_sp    = first.get("stopPoint") or {}
        platform   = rt_sp.get("platform") or first.get("track")
        stop_moved = bool(
            first.get("realtimeStopPoint")
            and first["realtimeStopPoint"].get("gid") != orig_sp.get("gid")
        )

        # Occupancy from API (requires includeOccupancy=true, which we set)
        occupancy = (first.get("occupancy") or {}).get("level")  # low / medium / high

        # Wheelchair accessibility from line metadata
        wheelchair = line.get("isWheelchairAccessible")

        # isRealtimeJourney: emergency/extra trip inserted at short notice (REST.md 6.2.3).
        # Per the v4 Swagger this flag lives on the LINE model, not serviceJourney.
        # When true, vehicle type info is unavailable.
        is_realtime_journey = line.get("isRealtimeJourney", False)

        # Line branding colours (for custom dashboard cards)
        bg_color     = line.get("backgroundColor")
        fg_color     = line.get("foregroundColor")
        border_color = line.get("borderColor")
        designation  = line.get("designation")          # public line number
        sub_mode     = line.get("transportSubMode")     # e.g. regionaltrain / vasttagen

        # Occupancy source: whether the crowding level is a prediction or realtime.
        occ_source = (first.get("occupancy") or {}).get("source")

        # Parsed direction details (via, service flags, replacement line, …).
        direction_extras = _direction_extras(sj)

        # Estimated arrival at the configured destination stop (from the coordinator).
        arrival_time = None
        arrival_in_minutes = None
        travel_minutes = None
        if next_arrival:
            arrival_time = next_arrival.get("arrival_hhmm")
            travel_minutes = next_arrival.get("duration_minutes")
            arr_dt = parse_dt(next_arrival.get("arrival_time"))
            if arr_dt:
                arrival_in_minutes = max(0, int((arr_dt - now()).total_seconds() // 60))

        # Upcoming departures list (up to 4)
        upcoming: list[dict] = []
        for dep in relevant[:4]:
            t = _best_departure_dt(dep)
            if t is None:
                continue
            up_rt = dep.get("realtimeStopPoint") or dep.get("stopPoint") or {}
            upcoming.append({
                "departure":         t.strftime("%H:%M"),
                "minutes_until":     max(0, int((t - now()).total_seconds() // 60)),
                "platform":          up_rt.get("platform") or dep.get("track"),
                # estimatedTime present = realtime data available for this specific departure
                "is_realtime":       dep.get("estimatedTime") is not None,
                "is_cancelled":      dep.get("isCancelled", False),
                "is_part_cancelled": dep.get("isPartCancelled", False),
            })

        self._departure_dt = dep_dt
        self._extra = {
            "departure_time":         dep_dt.strftime("%H:%M") if dep_dt else None,
            "minutes_until":          max(0, int((dep_dt - now()).total_seconds() // 60)) if dep_dt else None,
            "platform":               platform,
            "stop_moved":             stop_moved,
            "destination":            sj.get("direction"),
            "designation":            designation,
            "transport_mode":         line.get("transportMode"),
            "transport_sub_mode":     sub_mode,
            "delay_minutes":          delay_min,
            "is_realtime":            first.get("estimatedTime") is not None,
            "is_realtime_journey":    is_realtime_journey,
            "is_cancelled":           first.get("isCancelled", False),
            "is_part_cancelled":      first.get("isPartCancelled", False),
            "occupancy":              occupancy,
            "occupancy_source":       occ_source,
            "wheelchair_accessible":  wheelchair,
            "line_color":             bg_color,
            "line_text_color":        fg_color,
            "line_border_color":      border_color,
            "details_reference":      first.get("detailsReference"),
            "service_journey_gid":    sj.get("gid"),
            # Estimated arrival at the configured destination stop
            "arrival_time":           arrival_time,
            "arrival_in_minutes":     arrival_in_minutes,
            "travel_minutes":         travel_minutes,
            "upcoming":               upcoming,
            **direction_extras,
        }


# ── Ticket sensor ──────────────────────────────────────────────────────────────

class VasttrafikTicketSensor(SensorEntity):
    """
    Cheapest adult single ticket price for the configured origin→destination journey.

    Only created when an end stop is configured.
    Updated every 30 minutes (prices rarely change intra-day).

    State: cheapest adult ticket price in SEK (float).
    Attributes: full ticket list including youth, zones, validity.
    """

    _attr_has_entity_name   = True
    _attr_name              = "Biljettpris"
    _attr_device_class      = SensorDeviceClass.MONETARY
    # A spot price is not an accumulating total — leave state_class unset so HA's
    # long-term statistics don't try to sum it (TOTAL was wrong here).
    _attr_native_unit_of_measurement = "SEK"
    _attr_icon              = "mdi:ticket"
    _attr_attribution       = "Data provided by Västtrafik"
    _attr_should_poll       = True

    def __init__(
        self,
        hass: HomeAssistant,
        api: VtjpAdapter,
        ml: dict,
        entry_id: str,
        idx: int,
    ) -> None:
        self.hass = hass
        self._api = api
        self._ml  = ml

        stop_gid  = ml.get(CONF_STOP_GID, "")
        line_name = ml.get(CONF_LINE_NAME, "")

        self._attr_unique_id   = f"{entry_id}_ticket_{stop_gid}_{line_name}_{dir_key_for_line(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        self._price: float | None = None
        self._extra: dict[str, Any] = {}
        # Throttle: only call the API every 30 minutes (prices change rarely)
        self._last_fetch: datetime | None = None
        self._fetch_interval = timedelta(minutes=30)

    @property
    def native_value(self) -> float | None:
        return self._price

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "origin":      self._ml.get(CONF_STOP_NAME),
            "destination": self._ml.get(CONF_END_STOP_NAME),
            **self._extra,
        }

    async def async_update(self) -> None:
        # Only fetch every 30 minutes — ticket prices change very rarely
        current = now()
        if self._last_fetch is not None and (current - self._last_fetch) < self._fetch_interval:
            return

        origin_gid = self._ml.get(CONF_STOP_GID) or ""
        dest_gid   = self._ml.get(CONF_END_STOP_GID) or ""
        if not origin_gid or not dest_gid:
            return

        def _fetch() -> list[dict]:
            return self._api.get_journey_ticket(origin_gid, dest_gid)

        try:
            tickets = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:
            _LOGGER.debug("Ticket fetch failed for %s: %s", self._attr_unique_id, exc)
            return

        if not tickets:
            return

        # TicketSpecificationApiModel[].configurations[] — verified against the v4 Swagger.
        # Pick the cheapest adult fare; expose the full breakdown as an attribute.
        cheapest: float | None = None
        structured: list[dict] = []
        for ticket in tickets:
            for cfg in (ticket.get("configurations") or []):
                price     = cfg.get("itemPrice")
                age_type  = cfg.get("ageType", "")
                validity  = cfg.get("validityLength")
                zones     = cfg.get("zoneIds") or []
                if price is not None:
                    price_f = float(price)
                    if age_type == "adult" and (cheapest is None or price_f < cheapest):
                        cheapest = price_f
                    structured.append({
                        "ticket_name":   ticket.get("ticketName"),
                        "product_type":  ticket.get("productType"),
                        "age_type":      age_type,
                        "price_sek":     price_f,
                        "validity":      validity,
                        "zones":         zones,
                    })

        self._price = cheapest
        self._extra = {"tickets": structured}
        self._last_fetch = now()


