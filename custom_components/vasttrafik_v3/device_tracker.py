"""Device tracker — live vehicle position for each monitored line.

Tier 1: /positions filtered by the matched departure's detailsReference (live
GPS). Tier 2 fallback: interpolate along the journey's GPS path using stop
call times.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import now as ha_now

from .api import VtjpAdapter
from ._helpers import parse_dt, to_float
from .const import (
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_DIRECTION_GID,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_MODE,
    DEFAULT_DELAY,
    DOMAIN,
    VEHICLE_SCAN_INTERVAL,
)
from .sensor import _MODE_ICON, device_info_for_line, dir_key_for_line

_LOGGER     = logging.getLogger(__name__)
_LOOKBACK   = timedelta(minutes=10)
_BBOX_DEG   = 0.15   # bbox half-width around the start stop, in degrees


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    api: VtjpAdapter = store["api"]
    coordinators = store["coordinators"]

    entities = [
        VasttrafikVehicleTracker(hass, api, coordinators[i], ml, entry.entry_id, i)
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    ]
    if not entities:
        return

    async_add_entities(entities, update_before_add=True)

    async def _tick(_dt: Any = None) -> None:
        for tracker in entities:
            # One tracker's failure must not stop the others updating this tick.
            try:
                await tracker.async_update()
                tracker.async_write_ha_state()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Vehicle tracker update failed for %s", tracker.entity_id)

    entry.async_on_unload(
        async_track_time_interval(hass, _tick, VEHICLE_SCAN_INTERVAL)
    )


class VasttrafikVehicleTracker(TrackerEntity):
    """Live vehicle position tracker, pinned to the exact configured line + direction."""

    _attr_has_entity_name = True
    _attr_name            = "Position"
    _attr_attribution     = "Data provided by Västtrafik"
    _attr_should_poll     = False

    def __init__(
        self,
        hass: HomeAssistant,
        api: VtjpAdapter,
        coordinator: Any,
        ml: dict,
        entry_id: str,
        idx: int,
    ) -> None:
        self.hass = hass
        self._api = api
        self._coordinator = coordinator
        self._ml  = ml

        stop_gid    = ml.get(CONF_STOP_GID, "")
        line_name   = ml.get(CONF_LINE_NAME, "")
        # Same dir_key as the sensor so both entities land in one device group.
        dir_key     = dir_key_for_line(ml)

        self._attr_unique_id   = f"{entry_id}_vt_{stop_gid}_{line_name}_{dir_key}"
        self._attr_device_info = device_info_for_line(entry_id, ml)

        mode = (ml.get(CONF_TRANSPORT_MODE) or "bus").lower()
        self._attr_icon = _MODE_ICON.get(mode, "mdi:bus")

        self._delay = timedelta(minutes=ml.get(CONF_DELAY, DEFAULT_DELAY))
        self._lat:  float | None = None
        self._lon:  float | None = None
        self._available = False
        self._extra: dict[str, Any] = {}

        # Stop coordinates — captured once from the first departure's stopPoint
        self._stop_lat: float | None = None
        self._stop_lon: float | None = None

        # Latched on first 404/501 from /positions to skip further retries.
        self._positions_unavailable = False
        self._position_notes: list[str] = []

        # Cache the journey path per detailsReference: fetch once per trip, not per tick.
        self._path_cache_ref: str | None = None
        self._path_cache: tuple[list[dict], list[dict]] | None = None

    # ── TrackerEntity ─────────────────────────────────────────────────────────

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self._lat

    @property
    def longitude(self) -> float | None:
        return self._lon

    @property
    def location_accuracy(self) -> int:
        return 10 if self._extra.get("position_source") == "realtime_gps" else 75

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._extra

    # ── Update ────────────────────────────────────────────────────────────────

    async def async_update(self) -> None:
        stop_gid       = self._ml[CONF_STOP_GID]
        line_name      = self._ml[CONF_LINE_NAME]
        direction_str  = self._ml.get(CONF_DIRECTION) or None
        current_time   = ha_now()

        # Reuse the shared coordinator's look-back window; no own fetch here.
        departures = (self._coordinator.data or {}).get("departures") or []
        if not departures:
            self._available = False
            return

        # Prefer realtimeStopPoint: the boarding stop can be relocated live.
        if self._stop_lat is None and departures:
            sp = departures[0].get("realtimeStopPoint") or departures[0].get("stopPoint") or {}
            self._stop_lat = to_float(sp.get("latitude"))
            self._stop_lon = to_float(sp.get("longitude"))

        dep, details_ref = _find_departure(
            departures, line_name, direction_str, current_time
        )
        if dep is None:
            self._available = False
            dir_label = direction_str or "any direction"
            self._extra = {
                "status": f"No active service for line {line_name} ({dir_label})"
            }
            return

        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}
        mode = (line.get("transportMode") or "bus").upper()
        self._attr_icon = {
            "BUS": "mdi:bus", "TRAM": "mdi:tram",
            "TRAIN": "mdi:train", "FERRY": "mdi:ferry", "TAXI": "mdi:taxi",
        }.get(mode, "mdi:bus")

        dep_time = parse_dt(
            dep.get("estimatedOtherwisePlannedTime")
            or dep.get("estimatedTime")
            or dep.get("plannedTime")
        )

        # Tier 1: live GPS pinned to this exact detailsReference.
        if not self._positions_unavailable and self._stop_lat is not None and details_ref:
            pos = await self._try_positions(details_ref)
            if pos is not None:
                self._lat, self._lon = pos
                self._available = True
                self._extra = {
                    "line":             line.get("shortName"),
                    "transport_mode":   line.get("transportMode"),
                    "direction":        sj.get("direction"),
                    "details_reference": details_ref,
                    "departed_at":      dep_time.strftime("%H:%M") if dep_time else None,
                    "position_source":  "realtime_gps",
                    "notes":            self._position_notes,
                }
                return

        # Tier 2: interpolate along the journey's GPS path.
        if not details_ref:
            self._available = False
            self._extra = {"status": "No detailsReference — cannot fetch journey path"}
            return

        path_data = await self._fetch_journey_path(stop_gid, details_ref)
        if path_data is None:
            self._available = False
            return

        coords, calls = path_data
        pos = _interpolate_on_path(coords, calls, current_time)

        if pos is None:
            self._available = False
            self._extra = {"status": "Insufficient stop coordinates for interpolation"}
            return

        self._lat, self._lon = pos
        self._available = True

        self._extra = {
            "line":              line.get("shortName"),
            "transport_mode":    line.get("transportMode"),
            "direction":         sj.get("direction"),
            "details_reference": details_ref,
            "departed_at":       dep_time.strftime("%H:%M") if dep_time else None,
            "current_segment":   _segment_label(calls, current_time),
            "next_stop":         _next_stop_name(calls, current_time),
            "progress_percent":  _progress_percent(calls, current_time),
            "route_points":      len(coords),
            "total_stops":       len(calls),
            "position_source":   "path_interpolation",
        }

    # ── Tier 1 ────────────────────────────────────────────────────────────────

    async def _try_positions(self, details_ref: str) -> tuple[float, float] | None:
        """Query /positions filtered to this detailsReference — the bbox plus the
        reference isolate the one vehicle even when several run the same line."""
        lat = self._stop_lat
        lon = self._stop_lon
        if lat is None or lon is None:
            return None

        ll = (lat - _BBOX_DEG, lon - _BBOX_DEG)
        ur = (lat + _BBOX_DEG, lon + _BBOX_DEG)

        def _fetch() -> list[dict]:
            return self._api.get_vehicle_positions(
                lower_left=ll,
                upper_right=ur,
                details_references=[details_ref],
            )

        try:
            positions = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:
            _LOGGER.debug("/positions fetch failed: %s", exc)
            self._positions_unavailable = True
            return None

        if not positions:
            return None

        pos = positions[0]
        self._position_notes = [
            n.get("text") for n in (pos.get("notes") or []) if n.get("text")
        ]
        lat_v = to_float(pos.get("latitude"))
        lon_v = to_float(pos.get("longitude"))
        if lat_v is not None and lon_v is not None:
            direction = pos.get("direction") or ""
            _LOGGER.debug(
                "Realtime GPS for ref %s: %.5f, %.5f (%s)",
                details_ref[:16], lat_v, lon_v, direction,
            )
            return (lat_v, lon_v)

        return None

    # ── Tier 2 ────────────────────────────────────────────────────────────────

    async def _fetch_journey_path(
        self, stop_gid: str, details_ref: str
    ) -> tuple[list[dict], list[dict]] | None:
        """Fetch (coords, calls) for the trip, cached per detailsReference since
        the route geometry is fixed for the life of the trip."""
        if self._path_cache_ref == details_ref and self._path_cache is not None:
            return self._path_cache

        def _fetch() -> dict:
            return self._api.get_departure_details(
                stop_gid,
                details_ref,
                includes=["servicejourneycalls", "servicejourneycoordinates"],
            )

        try:
            data = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:
            _LOGGER.debug(
                "Departure details failed for %s: %s", details_ref[:16], exc, exc_info=True
            )
            return None

        sjs = data.get("serviceJourneys") or []
        if not sjs:
            return None
        sj = sjs[0]

        coords = sj.get("serviceJourneyCoordinates") or []
        calls  = sj.get("callsOnServiceJourney") or []

        if not coords or not calls:
            _LOGGER.debug(
                "Departure details for %s: coords=%d calls=%d (too few for interpolation)",
                details_ref[:16], len(coords), len(calls),
            )
            return None

        self._path_cache_ref = details_ref
        self._path_cache = (coords, calls)
        return coords, calls


# ─────────────────────────── Pure helpers ────────────────────────────────────

def _find_departure(
    departures: list[dict],
    line_name: str,
    direction_str: str | None,
    now: datetime,
) -> tuple[dict | None, str | None]:
    """Best departure + detailsReference for this line/direction. Line matches
    exactly; direction is a case-insensitive substring safety net. Prefer the
    most recently departed vehicle (en route now), else the soonest upcoming."""
    best_past:      tuple[dict, str] | None = None
    best_past_t:    datetime | None = None
    best_future:    tuple[dict, str] | None = None
    best_future_t:  datetime | None = None
    dir_lower = direction_str.lower() if direction_str else None

    for dep in departures:
        if dep.get("isCancelled"):
            continue
        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}

        if line.get("shortName") != line_name:
            continue

        if dir_lower:
            dep_direction = (sj.get("direction") or "").lower()
            if dep_direction and dir_lower not in dep_direction:
                continue

        ref = dep.get("detailsReference") or ""
        t   = parse_dt(
            dep.get("estimatedOtherwisePlannedTime")
            or dep.get("estimatedTime")
            or dep.get("plannedTime")
        )
        if t is None:
            continue

        if t <= now:
            # Latest past departure = the bus still en route, not the first seen.
            if best_past_t is None or t > best_past_t:
                best_past, best_past_t = (dep, ref), t
        else:
            if best_future_t is None or t < best_future_t:
                best_future, best_future_t = (dep, ref), t

    chosen = best_past or best_future
    return (chosen[0], chosen[1]) if chosen else (None, None)


def _call_dep_time(call: dict) -> datetime | None:
    return parse_dt(
        call.get("estimatedDepartureTime")
        or call.get("estimatedOtherwisePlannedDepartureTime")
        or call.get("plannedDepartureTime")
    )


def _call_arr_time(call: dict) -> datetime | None:
    return parse_dt(
        call.get("estimatedArrivalTime")
        or call.get("estimatedOtherwisePlannedArrivalTime")
        or call.get("plannedArrivalTime")
    )


def _dist(a: dict, b: dict) -> float:
    dlat = (a.get("latitude") or 0) - (b.get("latitude") or 0)
    dlon = (a.get("longitude") or 0) - (b.get("longitude") or 0)
    return math.sqrt(dlat * dlat + dlon * dlon)


def _nearest_coord_idx(coords: list[dict], lat: float, lon: float) -> int:
    target = {"latitude": lat, "longitude": lon}
    best_i, best_d = 0, float("inf")
    for i, c in enumerate(coords):
        d = _dist(c, target)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def _interpolate_on_path(
    coords: list[dict],
    calls: list[dict],
    current_time: datetime,
) -> tuple[float, float] | None:
    """Interpolate position along the GPS breadcrumb path between consecutive stops."""
    if not coords:
        return None

    n = len(calls)
    if n < 2:
        sp  = (calls[0].get("stopPoint") or {}) if calls else {}
        lat = to_float(sp.get("latitude"))
        lon = to_float(sp.get("longitude"))
        return (lat, lon) if lat is not None else None

    # Before journey start
    first_dep = _call_dep_time(calls[0])
    if first_dep and current_time < first_dep:
        sp  = calls[0].get("stopPoint") or {}
        lat = to_float(sp.get("latitude"))
        lon = to_float(sp.get("longitude"))
        if lat is not None:
            return (lat, lon)
        c = coords[0]
        return (to_float(c.get("latitude")), to_float(c.get("longitude")))  # type: ignore[return-value]

    for i in range(n - 1):
        dep_a = _call_dep_time(calls[i])
        arr_b = _call_arr_time(calls[i + 1])
        if dep_a is None or arr_b is None:
            continue
        if not (dep_a <= current_time <= arr_b):
            continue

        total   = (arr_b - dep_a).total_seconds()
        elapsed = (current_time - dep_a).total_seconds()
        t = min(1.0, elapsed / total) if total > 0 else 0.0

        sp_a  = calls[i].get("stopPoint") or {}
        sp_b  = calls[i + 1].get("stopPoint") or {}
        lat_a = to_float(sp_a.get("latitude"))
        lon_a = to_float(sp_a.get("longitude"))
        lat_b = to_float(sp_b.get("latitude"))
        lon_b = to_float(sp_b.get("longitude"))

        if None in (lat_a, lon_a, lat_b, lon_b):
            break  # fall through to straight-line at end

        idx_a = _nearest_coord_idx(coords, lat_a, lon_a)  # type: ignore[arg-type]
        idx_b = _nearest_coord_idx(coords, lat_b, lon_b)  # type: ignore[arg-type]

        if idx_b <= idx_a:
            # Degenerate segment — straight-line fallback
            return (lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t)  # type: ignore[operator]

        segment = coords[idx_a : idx_b + 1]

        f  = t * (len(segment) - 1)
        lo = int(f)
        hi = min(lo + 1, len(segment) - 1)
        p  = f - lo

        c_lo = segment[lo]
        c_hi = segment[hi]
        lat_r = (to_float(c_lo.get("latitude"))  or 0.0) + ((to_float(c_hi.get("latitude"))  or 0.0) - (to_float(c_lo.get("latitude"))  or 0.0)) * p
        lon_r = (to_float(c_lo.get("longitude")) or 0.0) + ((to_float(c_hi.get("longitude")) or 0.0) - (to_float(c_lo.get("longitude")) or 0.0)) * p
        return (lat_r, lon_r)

    # After last stop
    sp  = calls[-1].get("stopPoint") or {}
    lat = to_float(sp.get("latitude"))
    lon = to_float(sp.get("longitude"))
    if lat is not None:
        return (lat, lon)
    c = coords[-1]
    lat_c = to_float(c.get("latitude"))
    lon_c = to_float(c.get("longitude"))
    return (lat_c, lon_c) if lat_c is not None else None


def _segment_label(calls: list[dict], t: datetime) -> str | None:
    for i in range(len(calls) - 1):
        dep_a = _call_dep_time(calls[i])
        arr_b = _call_arr_time(calls[i + 1])
        if dep_a and arr_b and dep_a <= t <= arr_b:
            a = (calls[i].get("stopPoint") or {}).get("name", "?")
            b = (calls[i + 1].get("stopPoint") or {}).get("name", "?")
            return f"{a} → {b}"
    return None


def _next_stop_name(calls: list[dict], t: datetime) -> str | None:
    for call in calls:
        arr = _call_arr_time(call)
        if arr and arr > t:
            return (call.get("stopPoint") or {}).get("name")
    return None


def _progress_percent(calls: list[dict], t: datetime) -> int | None:
    n = len(calls)
    if n < 2:
        return None
    total_segs = n - 1
    for i in range(total_segs):
        dep_a = _call_dep_time(calls[i])
        arr_b = _call_arr_time(calls[i + 1])
        if dep_a and arr_b and dep_a <= t <= arr_b:
            total   = (arr_b - dep_a).total_seconds()
            elapsed = (t - dep_a).total_seconds()
            seg_p   = min(1.0, elapsed / total) if total > 0 else 0.0
            return int((i + seg_p) / total_segs * 100)
    return None

