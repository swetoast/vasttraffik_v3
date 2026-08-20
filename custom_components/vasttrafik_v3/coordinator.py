"""
Per-line departures coordinator, shared by the line's sensor and tracker.

data = {
  "departures":   [DepartureApiModel, ...],   # a look-back → look-ahead window
  "next_arrival": {...} | None,               # ETA at the configured end stop
}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as ha_now

from ._helpers import parse_dt, short_direction
from .api import VtjpAdapter
from .const import (
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_END_STOP_GID,
    CONF_LINE_NAME,
    CONF_STOP_GID,
    DEFAULT_DELAY,
    DEPARTURE_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

LOOKBACK = timedelta(minutes=10)        # past, so the tracker sees a just-departed vehicle
FUTURE_HORIZON = timedelta(minutes=60)  # future, beyond the walk-time delay


def _best_departure_dt(dep: dict) -> datetime | None:
    for key in ("estimatedOtherwisePlannedTime", "estimatedTime", "plannedTime"):
        dt = parse_dt(dep.get(key))
        if dt:
            return dt
    return None


class VasttrafikDepartureCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        api: VtjpAdapter,
        ml: dict,
        idx: int,
    ) -> None:
        self._api = api
        self._ml = ml
        self._delay = timedelta(minutes=ml.get(CONF_DELAY, DEFAULT_DELAY))
        # Cache the destination-arrival details call, keyed by detailsReference.
        self._eta_ref: str | None = None
        self._eta_calls: list[dict] | None = None

        stop = ml.get(CONF_STOP_GID, "")
        line = ml.get(CONF_LINE_NAME, "")
        super().__init__(
            hass,
            _LOGGER,
            name=f"vasttrafik departures[{idx}] {line}@{stop}",
            update_interval=DEPARTURE_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        stop_gid = self._ml[CONF_STOP_GID]

        # Window: (now − delay − lookback) .. (now + delay + horizon).
        fetch_from = ha_now() - self._delay - LOOKBACK
        span_minutes = int(
            (2 * self._delay + LOOKBACK + FUTURE_HORIZON).total_seconds() // 60
        )

        # No server-side directionGid: it pins to one terminus, so a line with
        # several destination variants in one travel direction (line 5:
        # Brämhult / Duvgatan / Sjöhagsvägen) loses most of its trips. Direction
        # is filtered client-side instead, which can fall back to any trip.
        def _fetch() -> list[dict]:
            return self._api.get_departures(
                stop_gid,
                when=fetch_from,
                limit=50,
                time_span_minutes=span_minutes,
            )

        try:
            departures = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailed(f"Departure fetch failed: {exc}") from exc

        result: dict = {"departures": departures, "next_arrival": None}

        if self._ml.get(CONF_END_STOP_GID):
            dep, dep_t = self._pick_next(departures)
            if dep is not None:
                result["next_arrival"] = await self._destination_arrival(dep, dep_t)

        return result

    def _pick_next(self, departures: list[dict]) -> tuple[dict | None, datetime | None]:
        """Earliest departure >= now+delay. Prefer the configured direction, but
        fall back to any (mirrors the sensor, since headsigns vary per trip)."""
        line_name = self._ml.get(CONF_LINE_NAME, "")
        direction = (self._ml.get(CONF_DIRECTION) or "").lower() or None
        target = ha_now() + self._delay

        def _earliest(require_direction: bool) -> tuple[dict | None, datetime | None]:
            best: dict | None = None
            best_t: datetime | None = None
            for dep in departures:
                if dep.get("isCancelled"):
                    continue
                sj = dep.get("serviceJourney") or {}
                line = sj.get("line") or {}
                if (line.get("shortName") or "") != line_name:
                    continue
                if require_direction and direction:
                    d = sj.get("direction") or ""
                    if d and short_direction(direction) != short_direction(d):
                        continue
                t = _best_departure_dt(dep)
                if t is None or t < target:
                    continue
                if best_t is None or t < best_t:
                    best, best_t = dep, t
            return best, best_t

        dep, dep_t = _earliest(True)
        if dep is None:
            dep, dep_t = _earliest(False)
        return dep, dep_t

    async def _destination_arrival(
        self, dep: dict, dep_t: datetime | None
    ) -> dict | None:
        """Estimated arrival at the configured end stop for the given departure."""
        end_gid = self._ml.get(CONF_END_STOP_GID)
        ref = dep.get("detailsReference")
        if not end_gid or not ref:
            return None

        if self._eta_ref == ref and self._eta_calls is not None:
            calls = self._eta_calls
        else:
            def _fetch_calls() -> dict:
                return self._api.get_departure_details(
                    self._ml[CONF_STOP_GID], ref, includes=["servicejourneycalls"]
                )

            try:
                data = await self.hass.async_add_executor_job(_fetch_calls)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Destination-arrival details failed: %s", exc)
                return None
            sjs = data.get("serviceJourneys") or []
            calls = ((sjs[0] or {}).get("callsOnServiceJourney") if sjs else []) or []
            self._eta_ref = ref
            self._eta_calls = calls

        for call in calls:
            sp = call.get("stopPoint") or {}
            area = sp.get("stopArea") or {}
            if area.get("gid") == end_gid or sp.get("gid") == end_gid:
                arr = parse_dt(
                    call.get("estimatedOtherwisePlannedArrivalTime")
                    or call.get("plannedArrivalTime")
                )
                if not arr:
                    continue
                # Skip a destination call upstream of boarding (loop / opposite
                # orientation) — it would give a negative travel time.
                if dep_t is not None and arr < dep_t:
                    continue
                duration = (
                    int((arr - dep_t).total_seconds() // 60) if dep_t else None
                )
                return {
                    "details_reference": ref,
                    "arrival_time": arr.isoformat(),
                    "arrival_hhmm": arr.strftime("%H:%M"),
                    "duration_minutes": duration,
                }
        return None
