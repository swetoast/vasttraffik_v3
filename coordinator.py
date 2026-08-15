"""
Shared departures coordinator.

One VasttrafikDepartureCoordinator exists per monitored line. Both the departure
sensor and the vehicle tracker read its `data` instead of each fetching the stop's
departures themselves — so the same stop-area departures are pulled once per
interval rather than two (or three) times.

`data` is a dict:
    {
      "departures":   [ DepartureApiModel, ... ],   # the raw look-back window
      "next_arrival": { ... } | None,               # ETA at the configured end stop
    }

The fetch window is deliberately wide: it starts in the past (so the tracker can
find a just-departed vehicle) and extends past the configured walk-time delay plus
a future horizon (so the departure sensor always sees the next catchable trip).

When an end stop is configured, the coordinator additionally resolves the estimated
arrival time at that stop for the next catchable departure (one cached details call),
so the departure sensor can show "arrives at your destination at HH:MM".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as ha_now

from ._helpers import parse_dt
from .api import VtjpAdapter
from .const import (
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_DIRECTION_GID,
    CONF_END_STOP_GID,
    CONF_LINE_NAME,
    CONF_STOP_GID,
    DEFAULT_DELAY,
    DEPARTURE_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# How far back to look so the tracker can find a just-departed vehicle.
LOOKBACK = timedelta(minutes=10)
# How far ahead (beyond the walk-time delay) the sensor should still see departures.
FUTURE_HORIZON = timedelta(minutes=60)


def _best_departure_dt(dep: dict) -> datetime | None:
    for key in ("estimatedOtherwisePlannedTime", "estimatedTime", "plannedTime"):
        dt = parse_dt(dep.get(key))
        if dt:
            return dt
    return None


class VasttrafikDepartureCoordinator(DataUpdateCoordinator):
    """Fetch departures for one monitored line once per interval, shared by its entities."""

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

        # 1-entry cache of the destination-arrival details call, keyed by detailsReference.
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
        direction_gid = self._ml.get(CONF_DIRECTION_GID) or None

        # Window: from (now − delay − lookback) [past, for the tracker] through
        # (now + delay + horizon) [future, for the sensor]. Length is therefore
        # 2·delay + lookback + horizon.
        fetch_from = ha_now() - self._delay - LOOKBACK
        span_minutes = int(
            (2 * self._delay + LOOKBACK + FUTURE_HORIZON).total_seconds() // 60
        )

        def _fetch() -> list[dict]:
            return self._api.get_departures(
                stop_gid,
                when=fetch_from,
                direction_gid=direction_gid,
                limit=30,
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
        """Earliest matching (line + direction) departure at or after now + delay."""
        line_name = self._ml.get(CONF_LINE_NAME, "")
        direction = (self._ml.get(CONF_DIRECTION) or "").lower() or None
        target = ha_now() + self._delay

        best: dict | None = None
        best_t: datetime | None = None
        for dep in departures:
            if dep.get("isCancelled"):
                continue
            sj = dep.get("serviceJourney") or {}
            line = sj.get("line") or {}
            if (line.get("shortName") or "") != line_name:
                continue
            if direction:
                d = (sj.get("direction") or "").lower()
                if d and direction not in d:
                    continue
            t = _best_departure_dt(dep)
            if t is None or t < target:
                continue
            if best_t is None or t < best_t:
                best, best_t = dep, t
        return best, best_t

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
                if arr:
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
