"""
Device tracker — live vehicle position for each monitored line.

Positioning strategy (two-tier)
────────────────────────────────

Tier 1 — /positions  (real-time GPS, pinned to the exact vehicle)
  GET /positions?lowerLeftLat=...&lowerLeftLong=...&upperRightLat=...
                &upperRightLong=...&detailsReferences=<ref>&limit=1

  The `detailsReferences` parameter from the OAS spec pins the query to the
  EXACT service journey that was selected for this tracker instance.  This means
  two trackers on the same line but different directions never cross-contaminate:
  each has its own detailsReference from the departure it matched.

  Bounding box: ±0.15° (≈ 12 km) around the start stop, derived from
  stopPoint.latitude/longitude in the departure response.

Tier 2 — departure details + GPS path interpolation  (no extra subscription)
  GET /stop-areas/{gid}/departures/{detailsReference}/details
      ?includes=servicejourneycalls,servicejourneycoordinates

  serviceJourneyCoordinates — dense GPS breadcrumbs along the full route.
  callsOnServiceJourney     — stop times with coordinates.
  Interpolation maps time progress between consecutive stops onto the
  corresponding sub-segment of the GPS path.

Direction correctness
──────────────────────
Direction filtering happens at three levels (belt-and-suspenders):
  1. API-level: `directionGid` passed to get_departures() narrows the response
     to only departures heading towards the configured destination.
  2. `_find_departure()`: additionally filters by direction string as a safeguard
     when direction_gid is not set.
  3. detailsReference pinning: Tier 1 uses the specific detailsReference of the
     matched departure, so even if two buses of the same line are in the bbox
     the wrong one is never returned.
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
from .sensor import _MODE_ICON, device_info_for_line

_LOGGER     = logging.getLogger(__name__)
_LOOKBACK   = timedelta(minutes=10)
_BBOX_DEG   = 0.15   # ≈ 12 km bounding box half-width


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    api: VtjpAdapter = store["api"]

    entities = [
        VasttrafikVehicleTracker(hass, api, ml, entry.entry_id, i)
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    ]
    if not entities:
        return

    async_add_entities(entities, update_before_add=True)

    async def _tick(_dt: Any = None) -> None:
        for tracker in entities:
            await tracker.async_update()
            tracker.async_write_ha_state()

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
        ml: dict,
        entry_id: str,
        idx: int,
    ) -> None:
        self.hass = hass
        self._api = api
        self._ml  = ml

        stop_gid    = ml.get(CONF_STOP_GID, "")
        line_name   = ml.get(CONF_LINE_NAME, "")
        # Match departure sensor's unique_id key structure so both share device group correctly
        dir_key     = ml.get(CONF_DIRECTION_GID) or ml.get("end_stop_gid") or "any"

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

        # Set True on first 404/501 from /positions to avoid repeated retries
        self._positions_unavailable = False

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
        direction_gid  = self._ml.get(CONF_DIRECTION_GID) or None
        direction_str  = self._ml.get(CONF_DIRECTION) or None
        current_time   = ha_now()
        fetch_from     = current_time - self._delay - _LOOKBACK

        # ── 1. Fetch departures filtered by direction at API level ────────────
        def _fetch_deps() -> list[dict]:
            return self._api.get_departures(
                stop_gid,
                when=fetch_from,
                direction_gid=direction_gid,   # server-side direction filter
                limit=20,
            )

        try:
            departures = await self.hass.async_add_executor_job(_fetch_deps)
        except Exception as exc:
            _LOGGER.debug("Tracker departure fetch failed: %s", exc, exc_info=True)
            self._available = False
            return

        # Capture stop coordinates — prefer realtimeStopPoint (REST.md 6.2.2: stop may be moved)
        if self._stop_lat is None and departures:
            sp = departures[0].get("realtimeStopPoint") or departures[0].get("stopPoint") or {}
            self._stop_lat = to_float(sp.get("latitude"))
            self._stop_lon = to_float(sp.get("longitude"))

        # ── 2. Find the specific departure for this line+direction ─────────────
        dep, details_ref = _find_departure(
            departures, line_name, direction_str, current_time
        )
        if dep is None:
            self._available = False
            dir_label = direction_str or (f"direction GID {direction_gid}" if direction_gid else "any direction")
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
            dep.get("estimatedOtherwisePlannedTime")   # REST.md 6.2.1: best time field
            or dep.get("estimatedTime")
            or dep.get("plannedTime")
        )

        # ── 3. Tier 1: /positions pinned to this exact detailsReference ────────
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
                }
                return

        # ── 4. Tier 2: departure details + GPS path interpolation ──────────────
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
        """
        Query /positions pinned to a specific detailsReference.

        OAS: the `detailsReferences` parameter filters to exactly the service
        journey identified by this reference.  Combined with the bounding box
        this guarantees we get the right vehicle regardless of how many buses
        of the same line are running simultaneously.
        """
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

        # The first (and should be only) result is our exact vehicle
        pos = positions[0]
        lat_v = to_float(pos.get("latitude"))   # top-level per OAS spec
        lon_v = to_float(pos.get("longitude"))   # top-level per OAS spec
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
        """Fetch departure details with service journey coordinates and calls."""
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

        return coords, calls


# ─────────────────────────── Pure helpers ────────────────────────────────────

def _find_departure(
    departures: list[dict],
    line_name: str,
    direction_str: str | None,
    now: datetime,
) -> tuple[dict | None, str | None]:
    """
    Return the best departure + detailsReference for this specific line and direction.

    Filtering (belt-and-suspenders):
      1. Line shortName must match exactly.
      2. If direction_str is configured, serviceJourney.direction must contain it
         (case-insensitive substring match — handles partial names like "Frölunda").
      3. Prefer a recently-departed vehicle over an upcoming one.
    """
    best_past:   tuple[dict, str] | None = None
    best_future: tuple[dict, str] | None = None
    dir_lower = direction_str.lower() if direction_str else None

    for dep in departures:
        if dep.get("isCancelled"):
            continue
        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}

        # ── Line filter ──────────────────────────────────────────────────────
        if line.get("shortName") != line_name:
            continue

        # ── Direction filter (client-side safety net) ─────────────────────
        if dir_lower:
            dep_direction = (sj.get("direction") or "").lower()
            if dep_direction and dir_lower not in dep_direction:
                continue

        ref = dep.get("detailsReference") or ""
        t   = parse_dt(
            dep.get("estimatedOtherwisePlannedTime")   # REST.md 6.2.1
            or dep.get("estimatedTime")
            or dep.get("plannedTime")
        )
        if t is None:
            continue

        if t <= now:
            if best_past is None:
                best_past = (dep, ref)
        elif best_future is None:
            best_future = (dep, ref)

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

