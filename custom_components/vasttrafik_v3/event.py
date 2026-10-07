"""Event platform — one entity per monitored line that fires when something
changes for an upcoming trip, so one automation can cover every kind of news.
"""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._helpers import line_key
from .const import CONF_MONITORED_LINES, DOMAIN
from .coordinator import VasttrafikDepartureCoordinator
from .sensor import device_info_for_line

EVENT_TYPES = [
    "platform_changed",
    "departure_cancelled",
    "stop_skipped",
    "disruption_started",
    "disruption_ended",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        VasttrafikLineEvent(store["coordinators"][i], ml, entry.entry_id)
        for i, ml in enumerate(store["config"].get(CONF_MONITORED_LINES, []))
    )


class VasttrafikLineEvent(CoordinatorEntity[VasttrafikDepartureCoordinator], EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "trip_alert"
    _attr_event_types     = EVENT_TYPES
    _attr_icon            = "mdi:bus-alert"
    _attr_attribution     = "Data provided by Västtrafik"

    def __init__(
        self, coordinator: VasttrafikDepartureCoordinator, ml: dict, entry_id: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id   = f"{entry_id}_event_{line_key(ml)}"
        self._attr_device_info = device_info_for_line(entry_id, ml)
        # Only what happens from now on is news.
        self._last_seq = max((a["seq"] for a in coordinator.alerts), default=0)

    @callback
    def _handle_coordinator_update(self) -> None:
        fresh = [a for a in self.coordinator.alerts if a["seq"] > self._last_seq]
        for alert in fresh:
            self._last_seq = alert["seq"]
            data = {k: v for k, v in alert.items() if k not in ("seq", "type")}
            self._trigger_event(alert["type"], data)
        if fresh:
            self.async_write_ha_state()
