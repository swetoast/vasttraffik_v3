"""
Binary sensor — Störning (disruption) monitor for each monitored line.

Störning v1 OAS 2.0 spec corrections vs previous implementation
────────────────────────────────────────────────────────────────
WRONG (old)                         CORRECT (from spec)
─────────────────────────────────── ────────────────────────────────────
GET /traffic-situations?lineGid=X   GET /traffic-situations/line/{gid}
GET /traffic-situations?stopAreaGid GET /traffic-situations/stoparea/{gid}
affectedLines[].shortName           affectedLines[].designation
affectedLines[].transportMode       affectedLines[].defaultTransportModeCode
affectedStopAreas                   affectedStopPoints  (with .stopAreaGid inside)
subSituations[].description         does not exist in Störning v1
validFromDate / validUntilDate      startTime / endTime
priority                            does not exist in Störning v1
(missing)                           affectedJourneys[]
(missing)                           affectedLines[].directions[]
(missing)                           affectedLines[].backgroundColor/textColor

Fetch strategy
───────────────
If CONF_LINE_GID is available: GET /traffic-situations/line/{gid}
  → server returns only situations for that line; no client-side filtering needed
  → includes affectedJourneys for the exact service journeys affected

If only CONF_STOP_GID available: GET /traffic-situations/stoparea/{gid}
  → returns all situations at the stop; client-side filter by designation

Then: if we know the current service_journey_gid from the departure sensor,
also query GET /traffic-situations/journey/{gid} and merge results.
This catches journey-specific disruptions (e.g. one specific trip cancelled).
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import VtjpAdapter
from .const import (
    CONF_LINE_GID,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    DISRUPTION_SCAN_INTERVAL,
    DOMAIN,
    SEVERITY_ORDER,
)
from .sensor import device_info_for_line

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = DISRUPTION_SCAN_INTERVAL

# Severity string values from the API are plain strings (not enum codes in spec)
# Map to icons — we uppercase and try known values, fall back gracefully
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
    entities = [
        VasttrafikDisruptionSensor(hass, api, ml, entry.entry_id, i)
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    ]
    if entities:
        async_add_entities(entities, update_before_add=True)


class VasttrafikDisruptionSensor(BinarySensorEntity):
    """
    ON when ≥ 1 active Störning disruption affects the configured line.

    State attributes expose the full structured disruption data including
    affected journeys, stop points, line colours, and directions — all
    mapped from the correct Störning v1 field names.
    """

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
        idx: int,
    ) -> None:
        self.hass  = hass
        self._api  = api
        self._ml   = ml

        stop_gid  = ml.get(CONF_STOP_GID, "")
        line_name = ml.get(CONF_LINE_NAME, "")

        self._attr_unique_id   = f"{entry_id}_dis_{stop_gid}_{line_name}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        self._disruptions: list[dict] = []

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
        return {
            "line":             self._ml.get(CONF_LINE_NAME),
            "stop":             self._ml.get(CONF_STOP_NAME),
            "disruption_count": len(self._disruptions),
            "worst_severity":   self._worst_severity(),
            "disruptions":      self._disruptions,
        }

    # ── Update ────────────────────────────────────────────────────────────────

    async def async_update(self) -> None:
        line_gid  = self._ml.get(CONF_LINE_GID) or None
        line_name = self._ml.get(CONF_LINE_NAME) or ""
        stop_gid  = self._ml.get(CONF_STOP_GID) or None

        raw: list[dict] = []

        if line_gid:
            # Best case: path-based endpoint returns only this line's situations
            def _fetch_by_line() -> list[dict]:
                return self._api.get_traffic_situations_for_line(line_gid)

            try:
                raw = await self.hass.async_add_executor_job(_fetch_by_line)
                _LOGGER.debug(
                    "Störning by line GID %s: %d situation(s)", line_gid, len(raw)
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Störning line fetch failed for %s: %s",
                    self._attr_unique_id, exc, exc_info=True,
                )
                return

        elif stop_gid:
            # Fallback: fetch by stop area, then filter client-side by designation
            def _fetch_by_stop() -> list[dict]:
                return self._api.get_traffic_situations_for_stoparea(stop_gid)

            try:
                raw = await self.hass.async_add_executor_job(_fetch_by_stop)
                _LOGGER.debug(
                    "Störning by stop GID %s: %d situation(s) before line filter",
                    stop_gid, len(raw),
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Störning stop fetch failed for %s: %s",
                    self._attr_unique_id, exc, exc_info=True,
                )
                return

            # Client-side filter: designation is the public line number (e.g. "5", "16")
            # Falls back to name if designation is absent
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

        self._disruptions = [_normalise(sit) for sit in raw]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _worst_severity(self) -> str | None:
        if not self._disruptions:
            return None

        def _idx(s: dict) -> int:
            sev = (s.get("severity") or "").upper()
            return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0

        return max(self._disruptions, key=_idx).get("severity")


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalise_line(raw_line: dict) -> dict:
    """
    Normalise a Störning v1 LineApiModel into a consistent shape.

    OAS fields:
      gid, name, designation (public number), defaultTransportModeCode,
      backgroundColor, textColor, directions[{gid, directionCode, name}],
      municipalities[{municipalityNumber, municipalityName}],
      affectedStopPointGids[]
    """
    return {
        "gid":              raw_line.get("gid"),
        "name":             raw_line.get("name"),
        "designation":      raw_line.get("designation"),       # public line number e.g. "5"
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
    """
    Normalise a Störning v1 StopPointApiModel.

    OAS fields:
      gid, name, shortName, stopAreaGid, stopAreaName,
      stopAreaShortName, municipalityName, municipalityNumber
    """
    return {
        "gid":               raw_sp.get("gid"),
        "name":              raw_sp.get("name"),
        "short_name":        raw_sp.get("shortName"),
        "stop_area_gid":     raw_sp.get("stopAreaGid"),
        "stop_area_name":    raw_sp.get("stopAreaName"),
        "municipality":      raw_sp.get("municipalityName"),
    }


def _normalise_journey(raw_j: dict) -> dict:
    """
    Normalise a Störning v1 JourneyApiModel.

    OAS fields:
      gid, departureDateTime, line (LineApiModel)
    """
    return {
        "gid":              raw_j.get("gid"),
        "departure":        raw_j.get("departureDateTime"),
        "line":             _normalise_line(raw_j["line"]) if raw_j.get("line") else None,
    }


def _normalise(raw: dict) -> dict:
    """
    Normalise a full Störning v1 TrafficSituationApiModel into a stable shape.

    OAS fields:
      situationNumber, creationTime, startTime, endTime,
      severity, title, description,
      affectedStopPoints[], affectedLines[], affectedJourneys[]
    """
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

