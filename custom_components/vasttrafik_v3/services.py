"""Actions that return data, for automations, scripts and voice assistants:
a journey search, a stop's departure or arrival board, and a line's vehicles.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from ._helpers import best_departure_dt, hhmm, parse_dt, parse_journey
from .api import VtjpAdapter
from .const import DOMAIN

MODES = ["tram", "bus", "ferry", "train", "taxi"]

SEARCH_JOURNEY_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Required("origin"): cv.string,
    vol.Required("destination"): cv.string,
    vol.Optional("via"): cv.string,
    vol.Optional("time"): cv.datetime,
    vol.Optional("arrive_by", default=False): cv.boolean,
    vol.Optional("only_direct", default=False): cv.boolean,
    vol.Optional("transport_modes"): vol.All(cv.ensure_list, [vol.In(MODES)]),
    vol.Optional("limit", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
})
STOP_BOARD_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Required("stop"): cv.string,
    vol.Optional("board", default="departures"): vol.In(["departures", "arrivals"]),
    vol.Optional("minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
    vol.Optional("line"): cv.string,
    vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
})
LINE_VEHICLES_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): cv.string,
    vol.Required("line"): cv.string,
    vol.Optional("stop"): cv.string,
    vol.Optional("radius_km", default=10): vol.All(vol.Coerce(float), vol.Range(min=1, max=50)),
})


def _adapter(hass: HomeAssistant, call: ServiceCall) -> VtjpAdapter:
    stores = hass.data.get(DOMAIN) or {}
    entry_id = call.data.get("config_entry_id")
    store = stores.get(entry_id) if entry_id else next(iter(stores.values()), None)
    if not store:
        raise ServiceValidationError("Västtrafik is not set up or not loaded")
    return store["api"]


async def _call(hass: HomeAssistant, func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await hass.async_add_executor_job(lambda: func(*args, **kwargs))
    except Exception as exc:
        raise HomeAssistantError(f"Västtrafik request failed: {exc}") from exc


async def _stop(hass: HomeAssistant, api: VtjpAdapter, value: str) -> dict:
    """A stop given as a 16-digit gid or a name; the best name match wins."""
    value = value.strip()
    if len(value) == 16 and value.isdigit():
        return {"gid": value, "name": value}
    results = await _call(hass, api.lookup_station, value)
    if not results:
        raise ServiceValidationError(f"No stop found for '{value}'")
    return results[0]


def _plain(value: Any) -> Any:
    """Make parsed data JSON-safe: datetimes become ISO strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def async_register_services(hass: HomeAssistant) -> None:
    async def search_journey(call: ServiceCall) -> ServiceResponse:
        api = _adapter(hass, call)
        origin = await _stop(hass, api, call.data["origin"])
        destination = await _stop(hass, api, call.data["destination"])
        via = await _stop(hass, api, call.data["via"]) if call.data.get("via") else None
        when = call.data.get("time")
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        plan = await _call(
            hass, api.plan_journey, origin["gid"], destination["gid"],
            when=when, limit=call.data["limit"], only_direct=call.data["only_direct"],
            transport_modes=call.data.get("transport_modes"), include_occupancy=True,
            via_gid=via["gid"] if via else None, arrive_by=call.data["arrive_by"],
        )
        journeys = [j for j in map(parse_journey, plan.get("results") or []) if j]
        for journey in journeys:
            journey["departure_time"] = hhmm(journey["departure"])
            journey["arrival_time"] = hhmm(journey["arrival"])
        return {
            "origin": origin.get("name"),
            "destination": destination.get("name"),
            "journeys": _plain(sorted(journeys, key=lambda j: j["departure"])),
        }

    async def stop_board(call: ServiceCall) -> ServiceResponse:
        api = _adapter(hass, call)
        stop = await _stop(hass, api, call.data["stop"])
        arrivals = call.data["board"] == "arrivals"
        fetch = api.get_arrivals if arrivals else api.get_departures
        rows = await _call(
            hass, fetch, stop["gid"], limit=call.data["limit"],
            time_span_minutes=call.data["minutes"], max_per_line_and_direction=call.data["limit"],
        )
        wanted = call.data.get("line")
        entries = []
        for row in rows:
            journey = row.get("serviceJourney") or {}
            line = journey.get("line") or {}
            moment, planned = best_departure_dt(row), parse_dt(row.get("plannedTime"))
            if moment is None or (wanted and (line.get("shortName") or "") != wanted):
                continue
            entries.append({
                "time": moment.isoformat(),
                "clock": hhmm(moment),
                "line": line.get("shortName"),
                "transport_mode": line.get("transportMode"),
                "origin" if arrivals else "direction": (
                    journey.get("origin") if arrivals else journey.get("direction")
                ),
                "platform": (row.get("realtimeStopPoint") or row.get("stopPoint") or {}).get("platform"),
                "delay_minutes": (
                    max(0, int((moment - planned).total_seconds() // 60)) if planned else None
                ),
                "is_realtime": row.get("estimatedTime") is not None,
                "is_cancelled": bool(row.get("isCancelled")),
                "occupancy": (row.get("occupancy") or {}).get("level"),
            })
        entries.sort(key=lambda e: e["time"])
        return {"stop": stop.get("name"), "board": call.data["board"], "entries": entries}

    async def line_vehicles(call: ServiceCall) -> ServiceResponse:
        api = _adapter(hass, call)
        if call.data.get("stop"):
            stop = await _stop(hass, api, call.data["stop"])
            lat, lon = stop.get("latitude"), stop.get("longitude")
        else:
            lat, lon = hass.config.latitude, hass.config.longitude
        if lat is None or lon is None:
            raise ServiceValidationError("No position to search around; give a stop name")
        d_lat = call.data["radius_km"] / 111.0
        d_lon = d_lat / max(0.1, math.cos(math.radians(lat)))
        positions = await _call(
            hass, api.get_vehicle_positions,
            lower_left=(lat - d_lat, lon - d_lon), upper_right=(lat + d_lat, lon + d_lon),
            line_designations=[call.data["line"]], limit=200,
        )
        return {
            "line": call.data["line"],
            "vehicles": [
                {
                    "latitude": p.get("latitude"),
                    "longitude": p.get("longitude"),
                    "direction": p.get("direction"),
                    "name": p.get("name"),
                    "details_reference": p.get("detailsReference"),
                }
                for p in positions
                if p.get("latitude") is not None and p.get("longitude") is not None
            ],
        }

    for name, handler, schema in (
        ("search_journey", search_journey, SEARCH_JOURNEY_SCHEMA),
        ("stop_board", stop_board, STOP_BOARD_SCHEMA),
        ("line_vehicles", line_vehicles, LINE_VEHICLES_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.ONLY
        )
