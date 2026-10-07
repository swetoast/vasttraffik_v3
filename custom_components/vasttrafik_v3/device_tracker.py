"""Device tracker — where the bus you are going to catch is right now.

Follows the departure the line's sensor points at. Tier 1: /positions filtered
by that trip's detailsReference. Tier 2 fallback: interpolate along the trip's
path using its stop call times.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import now as ha_now

from ._helpers import (
    best_departure_dt,
    boarding_index,
    hhmm,
    line_key,
    parse_dt,
    to_float,
)
from .api import VtjpAdapter
from .const import (
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_STOP_GID,
    CONF_TRANSPORT_MODE,
    DOMAIN,
    VEHICLE_SCAN_INTERVAL,
)
from .coordinator import VasttrafikDepartureCoordinator
from .sensor import device_info_for_line

_LOGGER     = logging.getLogger(__name__)
_BBOX_DEG   = 0.15   # bbox half-width around the start stop, in degrees
_POSITIONS_BACKOFF = timedelta(minutes=10)  # pause /positions after an error
_MODE_ICON  = {
    "bus": "mdi:bus", "tram": "mdi:tram", "train": "mdi:train",
    "ferry": "mdi:ferry", "taxi": "mdi:taxi",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    api: VtjpAdapter = store["api"]
    coordinators = store["coordinators"]

    entities = [
        VasttrafikVehicleTracker(hass, api, coordinators[i], ml, entry.entry_id)
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    ]
    if not entities:
        return

    async_add_entities(entities, update_before_add=True)
    shared = _SharedPositions(hass, api)

    async def _tick(_dt: Any = None) -> None:
        positions = await shared.fetch(entities)
        for tracker in entities:
            # One tracker's failure must not stop the others updating this tick.
            try:
                await tracker.async_update(positions)
                tracker.async_write_ha_state()
            except Exception:
                _LOGGER.exception("Vehicle tracker update failed for %s", tracker.entity_id)

    entry.async_on_unload(
        async_track_time_interval(hass, _tick, VEHICLE_SCAN_INTERVAL)
    )


class _SharedPositions:
    """One /positions request per tick for every tracker, instead of one each.

    Relies on each returned position carrying the detailsReference it was asked
    for. If the server answers without them, trackers go back to asking one by one.
    """

    def __init__(self, hass: HomeAssistant, api: VtjpAdapter) -> None:
        self._hass = hass
        self._api = api
        self._usable = True
        self._retry_at: datetime | None = None

    async def fetch(self, trackers: list[VasttrafikVehicleTracker]) -> dict[str, dict] | None:
        """detailsReference → position, or None when trackers must ask themselves."""
        if not self._usable:
            return None
        now = ha_now()
        if self._retry_at and now < self._retry_at:
            return {}
        wanted = [t.wanted_position() for t in trackers]
        wanted = [w for w in wanted if w]
        if not wanted:
            return {}
        refs = list(dict.fromkeys(ref for ref, _, _ in wanted))
        lower = (min(lat for _, lat, _ in wanted) - _BBOX_DEG, min(lon for _, _, lon in wanted) - _BBOX_DEG)
        upper = (max(lat for _, lat, _ in wanted) + _BBOX_DEG, max(lon for _, _, lon in wanted) + _BBOX_DEG)

        def _fetch() -> list[dict]:
            return self._api.get_vehicle_positions(
                lower_left=lower, upper_right=upper, details_references=refs, limit=200
            )

        try:
            positions = await self._hass.async_add_executor_job(_fetch)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("/positions fetch failed: %s", exc)
            self._retry_at = now + _POSITIONS_BACKOFF
            return {}
        self._retry_at = None
        by_ref = {p.get("detailsReference"): p for p in positions if p.get("detailsReference") in refs}
        if positions and not by_ref:
            self._usable = False
            return None
        return by_ref


class VasttrafikVehicleTracker(TrackerEntity):
    _attr_has_entity_name = True
    _attr_name            = "Position"
    _attr_attribution     = "Data provided by Västtrafik"
    _attr_should_poll     = False
    _attr_source_type     = SourceType.GPS
    _attr_available       = False

    def __init__(
        self,
        hass: HomeAssistant,
        api: VtjpAdapter,
        coordinator: VasttrafikDepartureCoordinator,
        ml: dict,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self._api = api
        self._coordinator = coordinator
        self._ml  = ml

        self._attr_unique_id   = f"{entry_id}_vt_{line_key(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)
        self._attr_icon = _MODE_ICON.get((ml.get(CONF_TRANSPORT_MODE) or "bus").lower(), "mdi:bus")
        self._attr_extra_state_attributes: dict[str, Any] = {}

        # Centre of the /positions bounding box, taken from the first departure.
        self._stop_lat: float | None = None
        self._stop_lon: float | None = None
        self._positions_retry_at: datetime | None = None
        self._position_notes: list[str] = []

    def _set_position(self, pos: tuple[float, float], accuracy: int, extra: dict) -> None:
        self._attr_latitude, self._attr_longitude = pos
        self._attr_location_accuracy = accuracy
        self._attr_extra_state_attributes = extra
        self._attr_available = True

    def _set_unavailable(self, status: str | None = None) -> None:
        self._attr_available = False
        if status:
            self._attr_extra_state_attributes = {"status": status}

    # ── Update ────────────────────────────────────────────────────────────────

    def wanted_position(self) -> tuple[str, float, float] | None:
        """(detailsReference, stop latitude, stop longitude) of the tracked trip."""
        dep = (self._coordinator.data or {}).get("tracked") or {}
        sp = dep.get("realtimeStopPoint") or dep.get("stopPoint") or {}
        lat, lon = to_float(sp.get("latitude")), to_float(sp.get("longitude"))
        ref = dep.get("detailsReference")
        return (ref, lat, lon) if ref and lat is not None and lon is not None else None

    async def async_update(self, shared: dict[str, dict] | None = None) -> None:
        current_time = ha_now()
        data = self._coordinator.data or {}
        dep = data.get("tracked")
        if dep is None:
            self._set_unavailable(
                f"No upcoming departure for line {self._ml.get(CONF_LINE_NAME)}"
            )
            return

        # Prefer realtimeStopPoint: the boarding stop can be relocated live.
        if self._stop_lat is None:
            sp = dep.get("realtimeStopPoint") or dep.get("stopPoint") or {}
            self._stop_lat = to_float(sp.get("latitude"))
            self._stop_lon = to_float(sp.get("longitude"))

        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}
        self._attr_icon = _MODE_ICON.get((line.get("transportMode") or "bus").lower(), "mdi:bus")
        details_ref = dep.get("detailsReference")
        dep_time = best_departure_dt(dep)
        common = {
            "line":              line.get("shortName"),
            "transport_mode":    line.get("transportMode"),
            "direction":         sj.get("direction"),
            "details_reference": details_ref,
            "departure_time":    hhmm(dep_time),
            "minutes_to_stop": (
                max(0, int((dep_time - current_time).total_seconds() // 60)) if dep_time else None
            ),
        }

        journey = await self._coordinator.async_journey(dep)
        coords, calls = journey if journey else ([], [])
        if calls:
            common.update({
                "stops_away":       _stops_away(calls, self._ml[CONF_STOP_GID], current_time),
                "current_segment":  _segment_label(calls, current_time),
                "next_stop":        _next_stop_name(calls, current_time),
                "progress_percent": _progress_percent(calls, current_time),
            })

        # Tier 1: live position pinned to this exact trip.
        positions_ok = (
            self._positions_retry_at is None or current_time >= self._positions_retry_at
        )
        pos = None
        if shared is not None:
            pos = self._read_position(shared.get(details_ref))
        elif positions_ok and self._stop_lat is not None and details_ref:
            pos = await self._try_positions(details_ref)
        if pos is not None:
            self._set_position(pos, 10, {
                **common, "position_source": "realtime_gps", "notes": self._position_notes,
            })
            return

        # Tier 2: interpolate along the trip's path.
        pos = _interpolate_on_path(coords, calls, current_time) if coords and calls else None
        if pos is None or pos[0] is None or pos[1] is None:
            self._set_unavailable("No position data for this trip")
            return
        self._set_position(pos, 75, {
            **common,
            "route_points":    len(coords),
            "total_stops":     len(calls),
            "position_source": "path_interpolation",
        })

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
        except Exception as exc:  # noqa: BLE001
            # Back off rather than give up: one timeout must not disable live
            # positions until the next restart.
            _LOGGER.debug("/positions fetch failed: %s", exc)
            self._positions_retry_at = ha_now() + _POSITIONS_BACKOFF
            return None
        self._positions_retry_at = None

        if not positions:
            return None

        return self._read_position(next(
            (p for p in positions if p.get("detailsReference") == details_ref),
            positions[0],
        ))

    def _read_position(self, pos: dict | None) -> tuple[float, float] | None:
        if not pos:
            return None
        lat_v = to_float(pos.get("latitude"))
        lon_v = to_float(pos.get("longitude"))
        if lat_v is None or lon_v is None:
            return None
        self._position_notes = [
            n.get("text") for n in (pos.get("notes") or []) if n.get("text")
        ]
        return (lat_v, lon_v)


# ─────────────────────────── Pure helpers ────────────────────────────────────

def _stops_away(calls: list[dict], stop_gid: str, now: datetime) -> int | None:
    """Stops the vehicle still has to reach before yours: 0 means yours is next.
    None once it has passed."""
    board = boarding_index(calls, stop_gid)
    if board is None:
        return None
    heading_to = next(
        (i for i, call in enumerate(calls) if (_call_arr_time(call) or _call_dep_time(call) or now) > now),
        len(calls),
    )
    return board - heading_to if heading_to <= board else None


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

