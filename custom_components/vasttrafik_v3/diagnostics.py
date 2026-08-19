"""Diagnostics support for Västtrafik v3."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_KEY, CONF_MONITORED_LINES, CONF_SECRET, DOMAIN

TO_REDACT = {CONF_KEY, CONF_SECRET}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinators = store.get("coordinators", []) or []
    api = store.get("api")

    lines: list[dict[str, Any]] = []
    for idx, coord in enumerate(coordinators):
        data = coord.data or {}
        departures = data.get("departures") or []
        lines.append({
            "index": idx,
            "name": coord.name,
            "last_update_success": coord.last_update_success,
            "departure_count": len(departures),
            "next_arrival": data.get("next_arrival"),
        })

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "monitored_line_count": len(entry.data.get(CONF_MONITORED_LINES, [])),
        "disruptions_unavailable": bool(getattr(api, "disruptions_unavailable", False)),
        "coordinators": lines,
    }
