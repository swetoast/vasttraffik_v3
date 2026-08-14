"""Initialize the Västtrafik v3 integration."""
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
    try:
        await hass.async_add_executor_job(adapter.ensure_token)
    except ConfigEntryAuthFailed as exc:
        raise exc

    # One shared departures coordinator per monitored line. Both the departure
    # sensor and the vehicle tracker for that line read from it, so departures are
    # fetched once per interval instead of once per entity.
    coordinators: list[VasttrafikDepartureCoordinator] = []
    for idx, ml in enumerate(entry.data.get(CONF_MONITORED_LINES, [])):
        coordinator = VasttrafikDepartureCoordinator(hass, adapter, ml, idx)
        # Non-raising first refresh: a temporarily-unreachable stop leaves that
        # line's entities unavailable rather than blocking the whole integration.
        await coordinator.async_refresh()
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
    """
    Migrate an existing config entry to the current schema version.

    ConfigFlow.VERSION is 3. There have been no breaking data-schema changes, so
    entries at version ≤ 3 are simply adopted at the current version. Without this
    handler, an entry created by an older version would fail to load outright.
    """
    if entry.version > 3:
        # Downgrade (e.g. user rolled the integration back) — cannot safely migrate.
        _LOGGER.error(
            "Config entry version %s is newer than the integration supports (3)",
            entry.version,
        )
        return False

    if entry.version < 3:
        _LOGGER.debug("Migrating Västtrafik config entry from v%s to v3", entry.version)
        hass.config_entries.async_update_entry(entry, version=3)

    return True

