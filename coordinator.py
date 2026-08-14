"""
Shared departures coordinator.

One VasttrafikDepartureCoordinator exists per monitored line. Both the departure
sensor and the vehicle tracker read its `data` instead of each fetching the stop's
departures themselves — so the same stop-area departures are pulled once per
interval rather than two (or three) times.

The fetch window is deliberately wide: it starts in the past (so the tracker can
find a just-departed vehicle) and extends past the configured walk-time delay plus
a future horizon (so the departure sensor always sees the next catchable trip).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as ha_now

from .api import VtjpAdapter
from .const import (
    CONF_DELAY,
    CONF_DIRECTION_GID,
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

        stop = ml.get(CONF_STOP_GID, "")
        line = ml.get(CONF_LINE_NAME, "")
        super().__init__(
            hass,
            _LOGGER,
            name=f"vasttrafik departures[{idx}] {line}@{stop}",
            update_interval=DEPARTURE_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> list[dict]:
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
            return await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailed(f"Departure fetch failed: {exc}") from exc
