"""Västtrafik v3 integration setup."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import VtjpAdapter
from .const import (
    CONF_KEY,
    CONF_LANGUAGE,
    CONF_MONITORED_LINES,
    CONF_SECRET,
    DEFAULT_LANGUAGE,
    DOMAIN,
)
from .coordinator import VasttrafikDepartureCoordinator
from .options import options_update_listener

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[str] = ["sensor", "binary_sensor", "device_tracker"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    language = entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    adapter  = VtjpAdapter(entry.data[CONF_KEY], entry.data[CONF_SECRET], language=language)
    await hass.async_add_executor_job(adapter.ensure_token)

    # One shared departures coordinator per line, consumed by both its sensor and
    # its tracker. Refresh sequentially — the adapter shares a single
    # requests.Session and concurrent startup requests can race on the auth header.
    coordinators: list[VasttrafikDepartureCoordinator] = []
    for idx, ml in enumerate(entry.data.get(CONF_MONITORED_LINES, [])):
        coordinator = VasttrafikDepartureCoordinator(hass, adapter, ml, idx)
        await coordinator.async_refresh()  # non-raising; failed lines stay unavailable
        coordinators.append(coordinator)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": adapter,
        "config": entry.data,
        "coordinators": coordinators,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(options_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Adopt entries at version ≤ 3; without this an older entry fails to load."""
    if entry.version > 3:
        _LOGGER.error("Config entry version %s is newer than supported (3)", entry.version)
        return False
    if entry.version < 3:
        hass.config_entries.async_update_entry(entry, version=3)
    return True
