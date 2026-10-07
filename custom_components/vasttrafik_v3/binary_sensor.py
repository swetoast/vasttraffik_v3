"""Störning (disruption) binary sensor per monitored line.

Fetch by line gid when known (server-side filtered), else by stop area with a
client-side designation filter.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import now as ha_now

from ._helpers import dir_key_for_line, parse_dt
from .api import VtjpAdapter
from .coordinator import VasttrafikDepartureCoordinator
from .const import (
    CONF_LINE_GID,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_USE_DISRUPTIONS,
    DISRUPTION_SCAN_INTERVAL,
    DOMAIN,
    SEVERITY_ORDER,
)
from .sensor import device_info_for_line

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = DISRUPTION_SCAN_INTERVAL

_SEV_ICON: dict[str, str] = {
    "VERY_SEVERE": "mdi:alert-octagon",
    "SEVERE":      "mdi:alert-circle",
    "NORMAL":      "mdi:alert-circle-outline",
    "SLIGHT":      "mdi:information-outline",
    "UNKNOWN":     "mdi:help-circle-outline",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    api: VtjpAdapter = store["api"]
    # Clear any stale repair issue from an earlier build.
    ir.async_delete_issue(hass, DOMAIN, "storning_unavailable")
    if not store["switches"][CONF_USE_DISRUPTIONS]:
        return
    entities = [
        VasttrafikDisruptionSensor(hass, api, ml, entry.entry_id, store["coordinators"][i])
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    ]
    if entities:
        async_add_entities(entities, update_before_add=True)


class VasttrafikDisruptionSensor(BinarySensorEntity):
    """ON while a disruption affecting the configured line is in effect.

    The API also returns situations that start later; those are listed as
    upcoming but do not turn the sensor on.
    """

    # Can exceed the recorder's 16 kB attribute limit on a bad traffic day.
    _unrecorded_attributes = frozenset({"disruptions", "upcoming_disruptions"})

    _attr_has_entity_name  = True
    _attr_name             = "Störning"
    _attr_device_class     = BinarySensorDeviceClass.PROBLEM
    _attr_attribution      = "Data provided by Västtrafik"
    _attr_should_poll      = True

    def __init__(
        self,
        hass: HomeAssistant,
        api: VtjpAdapter,
        ml: dict,
        entry_id: str,
        coordinator: VasttrafikDepartureCoordinator,
    ) -> None:
        self.hass  = hass
        self._api  = api
        self._ml   = ml
        self._coordinator = coordinator
        self._known: set[str] | None = None  # active situation numbers at last poll

        stop_gid  = ml.get(CONF_STOP_GID, "")
        line_name = ml.get(CONF_LINE_NAME, "")

        self._attr_unique_id   = f"{entry_id}_dis_{stop_gid}_{line_name}_{dir_key_for_line(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        self._situations: list[dict] = []

    @property
    def _disruptions(self) -> list[dict]:
        return [s for s in self._situations if _is_active(s)]

    # ── BinarySensorEntity ────────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        return bool(self._disruptions)

    @property
    def icon(self) -> str:
        worst = self._worst_severity()
        if worst is None:
            return "mdi:check-circle-outline"
        return _SEV_ICON.get(worst.upper(), "mdi:alert-circle-outline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # scope: "trip" names one of your next buses, "stop" hits a stop you
        # use, "line" is somewhere else on the line.
        active = [
            {**s, "scope": self._coordinator.situation_scope(s)} for s in self._disruptions
        ]
        scopes = {s["scope"] for s in active}
        return {
            "line":                 self._ml.get(CONF_LINE_NAME),
            "stop":                 self._ml.get(CONF_STOP_NAME),
            "disruption_count":     len(active),
            "worst_severity":       self._worst_severity(),
            "affects_your_stop":    bool(scopes & {"stop", "trip"}),
            "affects_next_departure": "trip" in scopes,
            "disruptions":          active,
            "upcoming_disruptions": [s for s in self._situations if _is_upcoming(s)],
        }

    # ── Update ────────────────────────────────────────────────────────────────

    async def async_update(self) -> None:
        line_gid  = self._ml.get(CONF_LINE_GID) or None
        line_name = self._ml.get(CONF_LINE_NAME) or ""
        stop_gid  = self._ml.get(CONF_STOP_GID) or None

        raw: list[dict] = []

        if line_gid:
            def _fetch_by_line() -> list[dict]:
                return self._api.get_traffic_situations_for_line(line_gid)

            try:
                raw = await self.hass.async_add_executor_job(_fetch_by_line)
                _LOGGER.debug(
                    "Störning by line GID %s: %d situation(s)", line_gid, len(raw)
                )
            except Exception as exc:  # noqa: BLE001
                self._fetch_failed("line", exc)
                return

        elif stop_gid:
            def _fetch_by_stop() -> list[dict]:
                return self._api.get_traffic_situations_for_stoparea(stop_gid)

            try:
                raw = await self.hass.async_add_executor_job(_fetch_by_stop)
                _LOGGER.debug(
                    "Störning by stop GID %s: %d situation(s) before line filter",
                    stop_gid, len(raw),
                )
            except Exception as exc:  # noqa: BLE001
                self._fetch_failed("stop", exc)
                return

            # Filter by designation (public line number), falling back to name.
            if line_name:
                line_lower = line_name.lower()
                raw = [
                    sit for sit in raw
                    if any(
                        (aff.get("designation") or aff.get("name") or "").lower() == line_lower
                        for aff in (sit.get("affectedLines") or [])
                    )
                ]
                _LOGGER.debug(
                    "After line filter '%s': %d situation(s)", line_name, len(raw)
                )
        else:
            return

        # "off" must mean "no disruption", not "could not ask".
        if self._api.disruptions_unavailable:
            self._went_unavailable()
            return
        if not self._attr_available:
            _LOGGER.info("Störning data is available again for %s", self._attr_unique_id)
        self._attr_available = True
        self._situations = [_normalise(sit) for sit in raw]
        self._publish()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _publish(self) -> None:
        """Hand the active situations to the line's coordinator, which shows the
        relevant one on the departure sensor and reports changes as events."""
        active = self._disruptions
        coordinator = self._coordinator
        coordinator.situations = active
        numbers = {str(s.get("situation_number")) for s in active}
        if self._known is not None:
            for s in active:
                if str(s.get("situation_number")) not in self._known:
                    coordinator.push_alert(
                        "disruption_started",
                        title=s.get("title"), description=s.get("description"),
                        severity=s.get("severity"), scope=coordinator.situation_scope(s),
                        situation_number=s.get("situation_number"),
                    )
            for number in self._known - numbers:
                coordinator.push_alert("disruption_ended", situation_number=number)
        changed = self._known is not None and numbers != self._known
        self._known = numbers
        if changed or active:
            coordinator.async_update_listeners()

    def _fetch_failed(self, kind: str, exc: Exception) -> None:
        if self._attr_available:  # log once per outage, not every poll
            _LOGGER.warning(
                "Störning %s fetch failed for %s: %s", kind, self._attr_unique_id, exc
            )
        self._went_unavailable()

    def _went_unavailable(self) -> None:
        """Without fresh data the departure sensor must not keep showing an old
        disruption. `_known` is kept, so recovery does not re-announce it."""
        self._attr_available = False
        if self._coordinator.situations:
            self._coordinator.situations = []
            self._coordinator.async_update_listeners()

    def _worst_severity(self) -> str | None:
        if not self._disruptions:
            return None

        def _idx(s: dict) -> int:
            sev = (s.get("severity") or "").upper()
            return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0

        return max(self._disruptions, key=_idx).get("severity")


def _is_active(situation: dict) -> bool:
    now = ha_now()
    start = parse_dt(situation.get("start_time"))
    end = parse_dt(situation.get("end_time"))
    return (start is None or start <= now) and (end is None or now < end)


def _is_upcoming(situation: dict) -> bool:
    start = parse_dt(situation.get("start_time"))
    return start is not None and start > ha_now()


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalise_line(raw_line: dict) -> dict:
    return {
        "gid":              raw_line.get("gid"),
        "name":             raw_line.get("name"),
        "designation":      raw_line.get("designation"),
        "transport_mode":   raw_line.get("defaultTransportModeCode"),
        "background_color": raw_line.get("backgroundColor"),
        "text_color":       raw_line.get("textColor"),
        "directions": [
            {
                "gid":  d.get("gid"),
                "code": d.get("directionCode"),
                "name": d.get("name"),
            }
            for d in (raw_line.get("directions") or [])
        ],
        "affected_stop_point_gids": raw_line.get("affectedStopPointGids") or [],
    }


def _normalise_stop_point(raw_sp: dict) -> dict:
    return {
        "gid":               raw_sp.get("gid"),
        "name":              raw_sp.get("name"),
        "short_name":        raw_sp.get("shortName"),
        "stop_area_gid":     raw_sp.get("stopAreaGid"),
        "stop_area_name":    raw_sp.get("stopAreaName"),
        "municipality":      raw_sp.get("municipalityName"),
    }


def _normalise_journey(raw_j: dict) -> dict:
    return {
        "gid":              raw_j.get("gid"),
        "departure":        raw_j.get("departureDateTime"),
        "line":             _normalise_line(raw_j["line"]) if raw_j.get("line") else None,
    }


def _normalise(raw: dict) -> dict:
    lines    = [_normalise_line(l)        for l in (raw.get("affectedLines")    or [])]
    stops    = [_normalise_stop_point(s)  for s in (raw.get("affectedStopPoints") or [])]
    journeys = [_normalise_journey(j)     for j in (raw.get("affectedJourneys") or [])]

    return {
        "situation_number":  raw.get("situationNumber"),
        "created":           raw.get("creationTime"),
        "start_time":        raw.get("startTime"),
        "end_time":          raw.get("endTime"),
        "severity":          (raw.get("severity") or "UNKNOWN").upper(),
        "title":             raw.get("title") or "",
        "description":       raw.get("description") or "",
        "affected_lines":    lines,
        "affected_stops":    stops,
        "affected_journeys": journeys,
    }

