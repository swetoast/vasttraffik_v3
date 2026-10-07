"""Västtrafik v3 integration setup."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from ._helpers import line_key, parse_stop_info
from .api import VasttrafikConnectionError, VtjpAdapter
from .const import (
    API_SWITCHES,
    CONF_DELAY,
    CONF_END_STOP_GID,
    CONF_END_STOP_NAME,
    CONF_KEY,
    CONF_LANGUAGE,
    CONF_MONITORED_LINES,
    CONF_SECRET,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_USE_DISRUPTIONS,
    CONF_USE_HOME,
    CONF_USE_PARKING,
    DEFAULT_DELAY,
    DEFAULT_LANGUAGE,
    DOMAIN,
)
from .services import async_register_services
from .coordinator import (
    VasttrafikDepartureCoordinator,
    VasttrafikParkingCoordinator,
    VasttrafikRouteCoordinator,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[str] = ["sensor", "binary_sensor", "device_tracker", "event"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    language = entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    adapter  = VtjpAdapter(entry.data[CONF_KEY], entry.data[CONF_SECRET], language=language)
    # A rejected key raises ConfigEntryAuthFailed (reauth); an outage must
    # instead be retried, or a reboot without internet would demand new keys.
    try:
        await hass.async_add_executor_job(adapter.ensure_token)
    except VasttrafikConnectionError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    switches = await _api_switches(hass, entry, adapter)
    adapter.disabled_apis = {name for key, name in API_SWITCHES.items() if not switches[key]}

    lines = entry.data.get(CONF_MONITORED_LINES, [])
    _remove_stale_devices(hass, entry, lines, switches)

    # One shared departures coordinator per line, consumed by both its sensor and
    # its tracker. Refresh sequentially — the adapter shares a single
    # requests.Session and concurrent startup requests can race on the auth header.
    coordinators: list[VasttrafikDepartureCoordinator] = []
    for idx, ml in enumerate(lines):
        coordinator = VasttrafikDepartureCoordinator(hass, entry, adapter, ml, idx)
        await coordinator.async_refresh()  # non-raising; failed lines stay unavailable
        coordinators.append(coordinator)

    # Everything below is derived from the monitored lines, so it needs no setup
    # of its own: what Geografi and Pendelparkering know about the stops
    # involved, and a trip per stop pair.
    boarding_stops = list(dict.fromkeys(ml[CONF_STOP_GID] for ml in lines if ml.get(CONF_STOP_GID)))
    end_stops = [ml[CONF_END_STOP_GID] for ml in lines if ml.get(CONF_END_STOP_GID)]
    stops: dict[str, dict] = {}
    for gid in dict.fromkeys(boarding_stops + end_stops):
        try:
            stops[gid] = parse_stop_info(
                await hass.async_add_executor_job(adapter.stop_area_info, gid)
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Stop details unavailable for %s: %s", gid, exc)

    parking = VasttrafikParkingCoordinator(
        hass, entry, adapter, boarding_stops if switches[CONF_USE_PARKING] else []
    )
    await parking.async_refresh()
    stops_with_parking = {area["stop_gid"] for area in (parking.data or {}).values()}

    # The home coordinate leaves Home Assistant only if the user allows it. An
    # entry from before the switch existed never agreed, so it counts as off.
    home = (
        (hass.config.latitude, hass.config.longitude)
        if entry.data.get(CONF_USE_HOME, False)
        and (hass.config.latitude or hass.config.longitude) else None
    )
    routes: dict[tuple[str, str], VasttrafikRouteCoordinator] = {}
    for (origin, destination), (names, delay) in _stop_pairs(lines).items():
        route = VasttrafikRouteCoordinator(
            hass, entry, adapter, (origin, names[0]), (destination, names[1]), delay,
            home=home, origin_has_parking=origin in stops_with_parking,
        )
        await route.async_refresh()
        routes[(origin, destination)] = route

    def next_departure_from(stop_gid: str) -> datetime | None:
        times = [
            journey["departure"]
            for (origin, _), route in routes.items() if origin == stop_gid
            for journey in route.upcoming()[:1]
        ]
        return min(times) if times else None

    parking.next_departure_from = next_departure_from
    if parking.data:
        # Now that the trips are known, add the forecast for when you would park.
        parking.reset_forecast()
        await parking.async_refresh()

    # The camera platform is only loaded when a parking actually has cameras.
    has_cameras = any(
        lot["cameras"] for area in (parking.data or {}).values() for lot in area["lots"]
    )
    platforms = PLATFORMS + (["camera"] if has_cameras else [])

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": adapter,
        "platforms": platforms,
        "switches": switches,
        "config": entry.data,
        "coordinators": coordinators,
        "routes": routes,
        "stops": stops,
        "parking": parking,
    }
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    platforms = hass.data[DOMAIN].get(entry.entry_id, {}).get("platforms", PLATFORMS)
    ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if ok:
        store = hass.data[DOMAIN].pop(entry.entry_id, None)
        if store:
            await hass.async_add_executor_job(store["api"].close)
    return ok


async def _api_switches(
    hass: HomeAssistant, entry: ConfigEntry, adapter: VtjpAdapter
) -> dict[str, bool]:
    """Which APIs beyond Planera Resa to use. An entry from before these
    switches existed gets them from what its key can actually reach, once."""
    stored = {key: bool(entry.data[key]) for key in API_SWITCHES if key in entry.data}
    if len(stored) == len(API_SWITCHES):
        return stored
    access = await hass.async_add_executor_job(adapter.probe_optional_apis)
    switches = {key: stored.get(key, access[name] is not False) for key, name in API_SWITCHES.items()}
    if any(access[name] is None for key, name in API_SWITCHES.items() if key not in stored):
        return switches  # could not tell this time: decide on a later start
    off = [name for key, name in API_SWITCHES.items() if not switches[key]]
    if off:
        _LOGGER.info(
            "This API key has no access to: %s. Those parts are switched off; add the "
            "APIs to the application at developer.vasttrafik.se and switch them on in "
            "the integration's options", ", ".join(off),
        )
    hass.config_entries.async_update_entry(entry, data={**entry.data, **switches})
    return switches


@callback
def _remove_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, lines: list[dict], switches: dict[str, bool]
) -> None:
    """Drop what no longer belongs: the devices (and with them the entities) of
    lines removed in the options flow, and the entities of an API switched off.
    Otherwise they linger as unavailable forever."""
    prefix = f"{entry.entry_id}_"
    keep = {prefix + line_key(ml) for ml in lines}
    keep |= {f"{prefix}route_{origin}_{destination}" for origin, destination in _stop_pairs(lines)}
    parking = tuple(
        f"{prefix}parking_{ml.get(CONF_STOP_GID)}_" for ml in lines
    ) if switches[CONF_USE_PARKING] else ("\0",)
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ours = [ident for domain, ident in device.identifiers if domain == DOMAIN]
        if not any(ident in keep or ident.startswith(parking) for ident in ours):
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)

    # A "leave at" sensor only exists with a walk time; drop it when that is set
    # to 0. (A trip's is kept: it can also run on Västtrafik's own walk time.)
    no_walk = {f"{prefix}leave_{line_key(ml)}" for ml in lines if not ml.get(CONF_DELAY)}
    disruptions_off = not switches[CONF_USE_DISRUPTIONS]
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.unique_id in no_walk or (
            disruptions_off and entity.unique_id.startswith(f"{prefix}dis_")
        ):
            entities.async_remove(entity.entity_id)


def _stop_pairs(lines: list[dict]) -> dict[tuple[str, str], tuple[tuple[str, str], timedelta]]:
    """Distinct (boarding stop, end stop) pairs among the lines, with their names
    and the shortest walk time any of their lines is configured with."""
    pairs: dict[tuple[str, str], tuple[tuple[str, str], timedelta]] = {}
    for ml in lines:
        origin, destination = ml.get(CONF_STOP_GID), ml.get(CONF_END_STOP_GID)
        if not origin or not destination:
            continue
        delay = timedelta(minutes=ml.get(CONF_DELAY, DEFAULT_DELAY))
        names = (ml.get(CONF_STOP_NAME) or origin, ml.get(CONF_END_STOP_NAME) or destination)
        known = pairs.get((origin, destination))
        pairs[(origin, destination)] = (names, min(delay, known[1]) if known else delay)
    return pairs


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Adopt entries at version ≤ 3; without this an older entry fails to load."""
    if entry.version > 3:
        _LOGGER.error("Config entry version %s is newer than supported (3)", entry.version)
        return False
    if entry.version < 3:
        hass.config_entries.async_update_entry(entry, version=3)
    return True
