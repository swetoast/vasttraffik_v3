"""Initialize the Västtrafik v3 integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import VtjpAdapter
from .const import CONF_KEY, CONF_LANGUAGE, CONF_SECRET, DEFAULT_LANGUAGE, DOMAIN
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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": adapter,
        "config": entry.data,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(options_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok

