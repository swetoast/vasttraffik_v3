"""Sensor platform.

Per monitored line: next departure, time to leave, ticket price.
Per stop pair:      next trip on any line, and when to leave for it.
Per commuter parking near a boarding stop: free spaces and when it fills up.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now

from ._helpers import (
    best_departure_dt,
    hhmm,
    line_key,
    parse_dt,
)
from .api import VtjpAdapter
from .const import (
    CONF_DIRECTION,
    CONF_END_STOP_GID,
    CONF_END_STOP_NAME,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_NAME,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_MODE,
    DEPARTURE_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import (
    VasttrafikDepartureCoordinator,
    VasttrafikParkingCoordinator,
    VasttrafikRouteCoordinator,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = DEPARTURE_SCAN_INTERVAL

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

def device_info_for_line(entry_id: str, ml: dict) -> DeviceInfo:
    """Shared DeviceInfo grouping a line's three entities under one device."""
    line_name = ml.get(CONF_LINE_NAME, "")
    mode      = (ml.get(CONF_TRANSPORT_MODE) or "bus").lower()
    stop_name = ml.get(CONF_STOP_NAME, "")

    device_name = ml.get(CONF_NAME) or f"Linje {line_name} – {stop_name}"
    if ml.get(CONF_END_STOP_NAME):
        device_name = ml.get(CONF_NAME) or (
            f"Linje {line_name} – {stop_name} → {ml[CONF_END_STOP_NAME]}"
        )

    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{line_key(ml)}")},
        name=device_name,
        manufacturer="Västtrafik",
        model=_MODE_LABEL.get(mode, "Transit"),
        entry_type=DeviceEntryType.SERVICE,
    )



def device_info_for_route(entry_id: str, route: VasttrafikRouteCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_route_{route.origin_gid}_{route.destination_gid}")},
        name=f"{route.origin_name} → {route.destination_name}",
        manufacturer="Västtrafik",
        model="Trip",
        entry_type=DeviceEntryType.SERVICE,
    )


def device_info_for_parking(entry_id: str, area: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_parking_{area['stop_gid']}_{area['id']}")},
        name=f"Pendelparkering {area['name']}",
        manufacturer="Västtrafik",
        model="Commuter parking",
        entry_type=DeviceEntryType.SERVICE,
    )


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
    """Useful bits of serviceJourney.directionDetails; service_flags lists only true flags."""
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
    stops: dict[str, dict] = store["stops"]
    entities: list[SensorEntity] = []
    polled: list[SensorEntity] = []

    for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, [])):
        coordinator = coordinators[i]
        entities.append(VasttrafikDepartureSensor(coordinator, ml, entry.entry_id, stops))
        if coordinator.delay:
            entities.append(VasttrafikLeaveAtSensor(coordinator, ml, entry.entry_id))
        if ml.get(CONF_END_STOP_GID) and ml.get(CONF_STOP_GID):
            polled.append(VasttrafikTicketSensor(hass, api, ml, entry.entry_id, stops))

    for route in store["routes"].values():
        entities.append(VasttrafikTripSensor(route, entry.entry_id))
        if route.delay or route.from_home:
            entities.append(VasttrafikTripLeaveAtSensor(route, entry.entry_id))

    parking: VasttrafikParkingCoordinator = store["parking"]
    for area in (parking.data or {}).values():
        if area["free"] is not None:
            entities.append(VasttrafikParkingFreeSensor(parking, area, entry.entry_id))
            entities.append(VasttrafikParkingFullAtSensor(parking, area, entry.entry_id))

    # Coordinator entities already have data; asking them to update first would
    # trigger a second, redundant refresh of every coordinator at startup.
    async_add_entities(entities)
    async_add_entities(polled, update_before_add=True)


# ── Monitored line ─────────────────────────────────────────────────────────────

class VasttrafikDepartureSensor(CoordinatorEntity[VasttrafikDepartureCoordinator], SensorEntity):
    """Next departure (timestamp state) fed by the shared coordinator."""

    _attr_has_entity_name  = True
    _attr_name             = None          # primary entity → uses device name
    _attr_device_class     = SensorDeviceClass.TIMESTAMP
    _attr_attribution      = "Data provided by Västtrafik"

    def __init__(
        self,
        coordinator: VasttrafikDepartureCoordinator,
        ml: dict,
        entry_id: str,
        stops: dict[str, dict],
    ) -> None:
        super().__init__(coordinator)
        self._ml = ml
        self._local_service = (stops.get(ml.get(CONF_STOP_GID, "")) or {}).get("has_local_service")
        self._attr_unique_id   = f"{entry_id}_dep_{line_key(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        mode = (ml.get(CONF_TRANSPORT_MODE) or "bus").lower()
        self._attr_icon = _MODE_ICON.get(mode, "mdi:bus-clock")

        self._delay = coordinator.delay
        self._departure_dt: datetime | None = None
        self._extra: dict[str, Any] = {}
        self._process()  # coordinator data is already available

    @property
    def native_value(self) -> datetime | None:
        return self._departure_dt

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "line":           self._ml.get(CONF_LINE_NAME),
            "stop":           self._ml.get(CONF_STOP_NAME),
            "direction":      self.coordinator.ml.get(CONF_DIRECTION) or "any",
            "end_stop":       self._ml.get(CONF_END_STOP_NAME),
            "walk_minutes":   int(self._delay.total_seconds() // 60),
            **self._extra,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._process()
        self.async_write_ha_state()

    def _process(self) -> None:
        """Recompute state + attributes from the coordinator's matched departures."""
        data          = self.coordinator.data or {}
        next_arrival  = data.get("next_arrival")
        relevant      = self.coordinator.upcoming()

        # Shown whether or not a departure is left to catch: these are the reasons.
        disruption = self.coordinator.relevant_disruption()
        always = {
            "cancelled_departures": [
                hhmm(best_departure_dt(dep)) for dep in data.get("cancelled") or []
            ],
            "disruption":       (disruption or {}).get("title"),
            "disruption_scope": (disruption or {}).get("scope"),
        }
        if self._local_service is not None:
            always["local_service"] = self._local_service

        if not relevant:
            self._departure_dt = None
            self._extra = always
            return
        first   = relevant[0]
        dep_dt  = best_departure_dt(first)
        plan_dt = parse_dt(first.get("plannedTime"))
        sj      = first.get("serviceJourney") or {}
        line    = sj.get("line") or {}
        mode    = (line.get("transportMode") or "bus").upper()

        delay_min: int | None = None
        if dep_dt and plan_dt:
            delay_min = max(0, int((dep_dt - plan_dt).total_seconds() // 60))

        self._attr_icon = {
            "BUS": "mdi:bus-clock", "TRAM": "mdi:tram",
            "TRAIN": "mdi:train",   "FERRY": "mdi:ferry", "TAXI": "mdi:taxi",
        }.get(mode, "mdi:bus-clock")

        # realtimeStopPoint is populated when the stop is moved within its area.
        rt_sp      = first.get("realtimeStopPoint") or first.get("stopPoint") or {}
        orig_sp    = first.get("stopPoint") or {}
        platform   = rt_sp.get("platform") or first.get("track")
        stop_moved = bool(
            first.get("realtimeStopPoint")
            and first["realtimeStopPoint"].get("gid") != orig_sp.get("gid")
        )

        occupancy    = (first.get("occupancy") or {}).get("level")
        occ_source   = (first.get("occupancy") or {}).get("source")
        wheelchair   = line.get("isWheelchairAccessible")
        is_realtime_journey = line.get("isRealtimeJourney", False)  # flag is on the line
        bg_color     = line.get("backgroundColor")
        fg_color     = line.get("foregroundColor")
        border_color = line.get("borderColor")
        designation  = line.get("designation")
        sub_mode     = line.get("transportSubMode")
        direction_extras = _direction_extras(sj)

        # ETA at the configured destination (computed by the coordinator).
        arrival_time = None
        arrival_in_minutes = None
        travel_minutes = None
        if next_arrival and next_arrival.get("details_reference") == first.get("detailsReference"):
            arrival_time = next_arrival.get("arrival_hhmm")
            travel_minutes = next_arrival.get("duration_minutes")
            arr_dt = parse_dt(next_arrival.get("arrival_time"))
            if arr_dt:
                arrival_in_minutes = max(0, int((arr_dt - now()).total_seconds() // 60))

        upcoming: list[dict] = []
        for dep in relevant[:4]:
            t = best_departure_dt(dep)
            if t is None:
                continue
            up_rt = dep.get("realtimeStopPoint") or dep.get("stopPoint") or {}
            upcoming.append({
                "departure":         hhmm(t),
                "minutes_until":     max(0, int((t - now()).total_seconds() // 60)),
                "platform":          up_rt.get("platform") or dep.get("track"),
                "is_realtime":       dep.get("estimatedTime") is not None,
                "is_cancelled":      dep.get("isCancelled", False),
                "is_part_cancelled": dep.get("isPartCancelled", False),
            })

        self._departure_dt = dep_dt
        self._extra = {
            "departure_time":         hhmm(dep_dt),
            "minutes_until":          max(0, int((dep_dt - now()).total_seconds() // 60)) if dep_dt else None,
            "platform":               platform,
            "stop_moved":             stop_moved,
            "destination":            sj.get("direction"),
            "direction_matched":      bool(data.get("direction_matched")),
            "leave_at":               hhmm(dep_dt - self._delay) if dep_dt and self._delay else None,
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
            "arrival_time":           arrival_time,
            "arrival_in_minutes":     arrival_in_minutes,
            "travel_minutes":         travel_minutes,
            "upcoming":               upcoming,
            **direction_extras,
            **always,
        }



class VasttrafikLeaveAtSensor(CoordinatorEntity[VasttrafikDepartureCoordinator], SensorEntity):
    """When to walk out the door for the next departure: usable as a time trigger."""

    _attr_has_entity_name = True
    _attr_translation_key = "leave_at"
    _attr_device_class    = SensorDeviceClass.TIMESTAMP
    _attr_icon            = "mdi:walk"
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(
        self, coordinator: VasttrafikDepartureCoordinator, ml: dict, entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id   = f"{entry_id}_leave_{line_key(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

    @property
    def native_value(self) -> datetime | None:
        upcoming = self.coordinator.upcoming()
        departure = best_departure_dt(upcoming[0]) if upcoming else None
        return departure - self.coordinator.delay if departure else None


# ── Trip between two stops ─────────────────────────────────────────────────────

class VasttrafikTripSensor(CoordinatorEntity[VasttrafikRouteCoordinator], SensorEntity):
    """Next way to get from the boarding stop to the end stop, whatever the line."""

    _attr_has_entity_name = True
    _attr_name            = None
    _attr_device_class    = SensorDeviceClass.TIMESTAMP
    _attr_icon            = "mdi:routes-clock"
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(self, coordinator: VasttrafikRouteCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{entry_id}_trip_{coordinator.origin_gid}_{coordinator.destination_gid}"
        )
        self._attr_device_info = device_info_for_route(entry_id, coordinator)

    @property
    def native_value(self) -> datetime | None:
        upcoming = self.coordinator.upcoming()
        return upcoming[0]["departure"] if upcoming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        route = self.coordinator
        base = {
            "origin":       route.origin_name,
            "destination":  route.destination_name,
            "walk_minutes": int(route.delay.total_seconds() // 60),
            **(route.ticket or {}),
        }
        upcoming = route.upcoming()
        if not upcoming:
            return base
        first = upcoming[0]
        access, egress = first["access"] or {}, first["egress"] or {}
        if not route.delay and access.get("minutes") is not None:
            base["walk_minutes"] = access["minutes"]
        current = now()

        def minutes(moment: datetime | None) -> int | None:
            return max(0, int((moment - current).total_seconds() // 60)) if moment else None

        travel = (
            int((first["arrival"] - first["departure"]).total_seconds() // 60)
            if first["arrival"] else None
        )
        return {
            **base,
            "line":            first["line"],
            "lines":           first["lines"],
            "direction":       first["direction"],
            "transport_mode":  first["transport_mode"],
            "departure_time":  hhmm(first["departure"]),
            "minutes_until":   minutes(first["departure"]),
            "leave_at":        hhmm(route.leave_time(first)),
            "board_at":        first["board_at"],
            "alight_at":       first["alight_at"],
            "access_mode":     access.get("mode"),
            "access_minutes":  access.get("minutes"),
            "access_distance_m": access.get("distance_m"),
            "home_arrival_time": hhmm(egress.get("time")),
            "transfers":       first["transfers"],
            "arrival_time":    hhmm(first["arrival"]),
            "arrival_in_minutes": minutes(first["arrival"]),
            "travel_minutes":  travel,
            "changes":         first["changes"],
            "platform":        first["platform"],
            "delay_minutes":   first["delay_minutes"],
            "is_realtime":     first["is_realtime"],
            "is_part_cancelled": first["is_part_cancelled"],
            "risk_of_missing_connection": first["risk_of_missing_connection"],
            "occupancy":       first["occupancy"],
            "notes":           first["notes"],
            "legs":            first["legs"],
            "upcoming": [
                {
                    "departure": hhmm(j["departure"]),
                    "arrival":   hhmm(j["arrival"]),
                    "lines":     j["lines"],
                    "changes":   j["changes"],
                    "minutes_until": minutes(j["departure"]),
                }
                for j in upcoming[:5]
            ],
        }


class VasttrafikTripLeaveAtSensor(CoordinatorEntity[VasttrafikRouteCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "leave_at"
    _attr_device_class    = SensorDeviceClass.TIMESTAMP
    _attr_icon            = "mdi:walk"
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(self, coordinator: VasttrafikRouteCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{entry_id}_tripleave_{coordinator.origin_gid}_{coordinator.destination_gid}"
        )
        self._attr_device_info = device_info_for_route(entry_id, coordinator)

    @property
    def native_value(self) -> datetime | None:
        upcoming = self.coordinator.upcoming()
        return self.coordinator.leave_time(upcoming[0]) if upcoming else None


# ── Commuter parking ───────────────────────────────────────────────────────────

class _ParkingEntity(CoordinatorEntity[VasttrafikParkingCoordinator]):
    _attr_has_entity_name = True
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(self, coordinator: VasttrafikParkingCoordinator, area: dict, entry_id: str) -> None:
        super().__init__(coordinator)
        self._area_id = area["id"]
        self._attr_device_info = device_info_for_parking(entry_id, area)

    @property
    def _area(self) -> dict | None:
        return (self.coordinator.data or {}).get(self._area_id)

    @property
    def available(self) -> bool:
        return super().available and self._area is not None


class VasttrafikParkingFreeSensor(_ParkingEntity, SensorEntity):
    _attr_translation_key = "parking_free_spaces"
    _attr_state_class     = SensorStateClass.MEASUREMENT
    _attr_icon            = "mdi:parking"

    def __init__(self, coordinator: VasttrafikParkingCoordinator, area: dict, entry_id: str) -> None:
        super().__init__(coordinator, area, entry_id)
        self._attr_unique_id = f"{entry_id}_parkfree_{area['id']}"

    @property
    def native_value(self) -> int | None:
        return (self._area or {}).get("free")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        area = self._area or {}
        return {
            "capacity": area.get("capacity"),
            "free_at_departure": area.get("free_at_departure"),
            "forecast_for": area.get("forecast_for"),
            "lots": [
                {k: lot[k] for k in ("name", "capacity", "free", "barrier", "latitude", "longitude")}
                for lot in area.get("lots") or []
            ],
            **(area.get("details") or {}),
        }


class VasttrafikParkingFullAtSensor(_ParkingEntity, SensorEntity):
    """Forecast of when the parking fills up today; unknown if it is not expected to."""

    _attr_translation_key = "parking_full_at"
    _attr_device_class    = SensorDeviceClass.TIMESTAMP
    _attr_icon            = "mdi:car-clock"

    def __init__(self, coordinator: VasttrafikParkingCoordinator, area: dict, entry_id: str) -> None:
        super().__init__(coordinator, area, entry_id)
        self._attr_unique_id = f"{entry_id}_parkfull_{area['id']}"

    @property
    def native_value(self) -> datetime | None:
        return (self._area or {}).get("full_at")


# ── Ticket price ───────────────────────────────────────────────────────────────

class VasttrafikTicketSensor(SensorEntity):
    """Cheapest adult single ticket price (SEK) for origin→destination.

    Only created when an end stop is set; throttled to one fetch per 30 min.
    No state_class: a spot price must not be summed by HA statistics.
    """

    _attr_has_entity_name   = True
    _attr_name              = "Biljettpris"
    _attr_device_class      = SensorDeviceClass.MONETARY
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
        stops: dict[str, dict],
    ) -> None:
        self.hass = hass
        self._api = api
        self._ml  = ml
        self._zones = {
            "origin_zones": (stops.get(ml.get(CONF_STOP_GID, "")) or {}).get("tariff_zones"),
            "destination_zones": (stops.get(ml.get(CONF_END_STOP_GID, "")) or {}).get("tariff_zones"),
        }

        self._attr_unique_id   = f"{entry_id}_ticket_{line_key(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        self._price: float | None = None
        self._extra: dict[str, Any] = {}
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
            **{k: v for k, v in self._zones.items() if v},
            **self._extra,
        }

    async def async_update(self) -> None:
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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Ticket fetch failed for %s: %s", self._attr_unique_id, exc)
            return

        if not tickets:
            return

        # Cheapest adult fare; full breakdown exposed as an attribute.
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


