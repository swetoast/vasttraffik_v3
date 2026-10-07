"""Options flow for Västtrafik v3 — add or remove monitored lines."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from ._helpers import line_key, walk_minutes
from .api import VtjpAdapter
from .const import (
    API_SWITCHES,
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_DIRECTION_GID,
    CONF_END_STOP_GID,
    CONF_END_STOP_NAME,
    CONF_KEY,
    CONF_LANGUAGE,
    CONF_LINE_GID,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_NAME,
    CONF_SECRET,
    CONF_STOP_GID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_MODE,
    CONF_USE_HOME,
    DEFAULT_DELAY,
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)


# ── Pure helpers (duplicated from config_flow to avoid circular import) ───────

def _gid(loc: dict) -> str | None:
    return loc.get("gid") or loc.get("id") or loc.get("stopAreaGid")


def _stop_label(loc: dict) -> str:
    name = loc.get("name") or "Unknown stop"
    muni = loc.get("municipality") or ""
    return f"{name} – {muni}" if muni and muni.lower() not in name.lower() else name


async def async_nearby_stops(hass: HomeAssistant, adapter: VtjpAdapter | None) -> list[dict]:
    """Stops around Home Assistant's home location, offered as suggestions."""
    lat, lon = hass.config.latitude, hass.config.longitude
    if adapter is None or (not lat and not lon):
        return []
    try:
        return await hass.async_add_executor_job(adapter.nearby_stops, lat, lon)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nearby stop lookup failed: %s", exc)
        return []


def apis_schema(current: dict[str, bool]) -> vol.Schema:
    return vol.Schema({
        vol.Optional(key, default=current.get(key, True)): BooleanSelector()
        for key in API_SWITCHES
    })


def api_defaults(access: dict[str, bool | None]) -> dict[str, bool]:
    """Tick what the key can use; an API that could not be checked stays ticked."""
    return {key: access.get(name) is not False for key, name in API_SWITCHES.items()}


def refused_choice(choice: dict, access: dict[str, bool | None]) -> bool:
    """True if something is switched on that the key is known not to have."""
    return any(choice.get(key) and access.get(name) is False for key, name in API_SWITCHES.items())


def start_stop_schema(nearby: list[dict]) -> vol.Schema:
    """One field: pick a nearby stop or type any stop name."""
    if not nearby:
        selector: Any = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
    else:
        selector = SelectSelector(SelectSelectorConfig(
            options=[
                {
                    "value": stop["name"],
                    "label": f"{stop['name']} · {stop.get('straightLineDistanceInMeters') or 0} m",
                }
                for stop in nearby
            ],
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        ))
    return vol.Schema({vol.Required(CONF_STOP_NAME): selector})


def nearby_match(nearby: list[dict], name: str) -> tuple[str, int] | None:
    """(gid, estimated walk minutes) when *name* is one of the suggestions."""
    stop = next((s for s in nearby if s["name"] == name), None)
    if stop is None:
        return None
    return stop["gid"], min(30, walk_minutes(stop.get("straightLineDistanceInMeters")))


def _natural_sort_key(s: str) -> tuple[int, str]:
    num = "".join(c for c in s if c.isdigit())
    alpha = "".join(c for c in s if not c.isdigit())
    return (int(num) if num else 9999, alpha)


def _lines_from_departures(departures: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for dep in departures:
        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}
        short = (line.get("shortName") or line.get("name") or "").strip()
        if not short or short in seen:
            continue
        mode = (line.get("transportMode") or "bus").lower()
        icon = {"bus": "Bus", "tram": "Tram", "train": "Train", "ferry": "Ferry"}.get(mode, "Bus")
        seen[short] = {
            "short_name": short,
            "gid": line.get("gid"),
            "transport_mode": mode,
            "label": f"{icon} {short}",
        }
    return sorted(seen.values(), key=lambda x: _natural_sort_key(x["short_name"]))


def _details_ref_for(
    departures: list[dict], line_name: str, direction_str: str | None = None
) -> str:
    """Return a detailsReference for a departure matching line (+ optional direction)."""
    dir_lower = direction_str.lower() if direction_str else None
    for dep in departures:
        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}
        if (line.get("shortName") or "") != line_name:
            continue
        if dir_lower:
            d = (sj.get("direction") or "").lower()
            if d and dir_lower not in d:
                continue
        ref = dep.get("detailsReference")
        if ref:
            return ref
    return ""


def _directions_for_line(departures: list[dict], line_name: str) -> list[dict]:
    seen: dict[str, dict] = {}
    for dep in departures:
        sj   = dep.get("serviceJourney") or {}
        line = sj.get("line") or {}
        if (line.get("shortName") or "") != line_name:
            continue
        direction = (
            sj.get("direction") or ""
        ).strip()
        if not direction or direction in seen:
            continue
        # No terminus GID on v4 departures; resolved separately when needed.
        seen[direction] = {
            "direction": direction,
            "direction_gid": None,
            "label": f"to {direction}",
        }
    return list(seen.values())


# ─────────────────────────── Options flow ────────────────────────────────────

class VasttrafikOptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Store as _entry, not self.config_entry: the latter is a read-only
        # property on modern HA and assigning it 500s the options flow.
        self._entry = config_entry
        self._monitored: list[dict] = list(
            config_entry.data.get(CONF_MONITORED_LINES, [])
        )
        self._key:      str = config_entry.data.get(CONF_KEY,      "")
        self._secret:   str = config_entry.data.get(CONF_SECRET,   "")
        self._language: str = config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        self._use_home: bool = config_entry.data.get(CONF_USE_HOME, False)
        self._apis: dict[str, bool] = {
            key: config_entry.data.get(key, True) for key in API_SWITCHES
        }
        self._adapter: VtjpAdapter | None = None

        # Per-iteration state — cleared by _reset().
        self._start_name: str = ""
        self._start_gid:  str = ""
        self._end_name:   str = ""
        self._end_gid:    str = ""
        self._stop_candidates:  list[dict] = []
        self._stop_picker_for:  str = ""
        self._live_departures:  list[dict] = []
        self._available_lines:  list[dict] = []
        self._journey_line_dirs: dict[str, str] = {}
        self._nearby: list[dict] | None = None
        self._walk: int | None = None
        self._edit_index: int = 0
        self._line_name:    str = ""
        self._line_gid:     str = ""
        self._line_mode:    str = ""
        self._direction:    str = ""
        self._direction_gid:str = ""

    def _reset(self) -> None:
        self._start_name = self._start_gid = ""
        self._end_name   = self._end_gid   = ""
        self._stop_candidates = []
        self._stop_picker_for = ""
        self._live_departures = []
        self._available_lines = []
        self._journey_line_dirs = {}
        self._walk = None
        self._line_name = self._line_gid = self._line_mode = ""
        self._direction = self._direction_gid = ""

    async def _ensure_adapter(self) -> bool:
        if self._adapter:
            return True
        try:
            a = VtjpAdapter(self._key, self._secret, language=self._language)
            await self.hass.async_add_executor_job(a.ensure_token)
            self._adapter = a
            return True
        except Exception as exc:
            _LOGGER.error("Options: adapter init failed: %s", exc, exc_info=True)
            return False

    async def _resolve_direction_gid(self, line_name: str, direction_str: str) -> str:
        """Resolve the terminus stop-area gid for the chosen line+direction (best-effort)."""
        ref = _details_ref_for(self._live_departures, line_name, direction_str)
        if not ref or not self._adapter:
            return ""
        try:
            gid = await self.hass.async_add_executor_job(
                self._adapter.resolve_terminus_gid, self._start_gid, ref
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Options: directionGid resolution failed: %s", exc)
            return ""
        return gid or ""

    # ── Entry point ───────────────────────────────────────────────────────────

    async def async_step_init(self, user_input: dict | None = None) -> dict:
        return await self.async_step_menu()

    async def async_step_menu(self, user_input: dict | None = None) -> dict:
        if user_input is not None:
            action = user_input.get("action", "")
            if action == "add":
                return await self.async_step_start_stop()
            if action == "edit":
                return await self.async_step_edit()
            if action == "remove":
                return await self.async_step_remove()
            if action == "apis":
                return await self.async_step_apis()
            if action == "home":
                self._use_home = not self._use_home
                self._nearby = None
                return await self.async_step_menu()
            if action == "language":
                return await self.async_step_language()
            return self._save()

        options = [{"value": "add", "label": "Add a monitored line"}]
        if self._monitored:
            options.append({"value": "edit", "label": "Change walk time or name of a line"})
            options.append({"value": "remove", "label": "Remove a monitored line"})
        lang_label = SUPPORTED_LANGUAGES.get(self._language, self._language)
        options.append({"value": "language", "label": f"Language: {lang_label}"})
        options.append({
            "value": "home",
            "label": "Use home location for suggestions and door-to-door trips: "
                     + ("on (select to turn off)" if self._use_home else "off (select to turn on)"),
        })
        used = [name for key, name in API_SWITCHES.items() if self._apis[key]]
        options.append({
            "value": "apis",
            "label": "Extra Västtrafik APIs in use: " + (", ".join(used) or "none"),
        })
        options.append({"value": "save", "label": "Save and close"})

        return self.async_show_form(
            step_id="menu",
            data_schema=vol.Schema({
                vol.Required("action"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
        )

    async def async_step_language(self, user_input: dict | None = None) -> dict:
        """Change the API response language."""
        if user_input is not None:
            self._language = user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
            return await self.async_step_menu()

        lang_options = [
            {"value": code, "label": label}
            for code, label in SUPPORTED_LANGUAGES.items()
        ]
        return self.async_show_form(
            step_id="language",
            data_schema=vol.Schema({
                vol.Required(CONF_LANGUAGE, default=self._language): SelectSelector(
                    SelectSelectorConfig(
                        options=lang_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_apis(self, user_input: dict | None = None) -> dict:
        """Choose which APIs beyond Planera Resa to use. The key is asked what
        it has access to, so one it lacks cannot be switched on by mistake."""
        errors: dict = {}
        if not await self._ensure_adapter():
            errors["base"] = "cannot_connect"
            access: dict[str, bool | None] = {}
        else:
            access = await self.hass.async_add_executor_job(
                self._adapter.probe_optional_apis  # type: ignore[union-attr]
            )
        if user_input is not None and not errors:
            choice = {key: bool(user_input.get(key)) for key in API_SWITCHES}
            if refused_choice(choice, access):
                errors["base"] = "api_not_enabled"
            else:
                self._apis = choice
                return await self.async_step_menu()
        return self.async_show_form(
            step_id="apis",
            data_schema=apis_schema(
                {key: bool(user_input.get(key)) for key in API_SWITCHES}
                if user_input is not None else self._apis
            ),
            errors=errors,
        )

    # ── Add: start stop ───────────────────────────────────────────────────────

    async def async_step_start_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_STOP_NAME) or "").strip()
            if not name:
                errors["base"] = "station_required"
            elif not await self._ensure_adapter():
                errors["base"] = "cannot_connect"
            elif picked := nearby_match(self._nearby or [], name):
                self._start_name, (self._start_gid, self._walk) = name, picked
                return await self.async_step_end_stop()
            else:
                try:
                    results = await self.hass.async_add_executor_job(
                        self._adapter.lookup_station, name  # type: ignore[union-attr]
                    )
                    _LOGGER.debug("Options start stop %r → %d", name, len(results))
                except Exception as exc:
                    _LOGGER.error("Options start stop lookup error: %s", exc, exc_info=True)
                    results = []
                    errors["base"] = "cannot_connect"

                if not errors:
                    if not results:
                        errors["base"] = "station_not_found"
                    elif len(results) == 1:
                        self._start_name = results[0].get("name") or name
                        self._start_gid  = _gid(results[0]) or ""
                        return await self.async_step_end_stop()
                    else:
                        self._stop_candidates = results
                        self._stop_picker_for = "start"
                        return await self.async_step_pick_stop()

        if self._nearby is None:
            await self._ensure_adapter()
            self._nearby = (
                await async_nearby_stops(self.hass, self._adapter) if self._use_home else []
            )
        return self.async_show_form(
            step_id="start_stop",
            data_schema=start_stop_schema(self._nearby),
            description_placeholders={"example": "Brunnsparken, Göteborg"},
            errors=errors,
        )

    # ── Add: end stop (optional) ──────────────────────────────────────────────

    async def async_step_end_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_END_STOP_NAME) or "").strip()
            if name:
                if not await self._ensure_adapter():
                    errors["base"] = "cannot_connect"
                else:
                    try:
                        results = await self.hass.async_add_executor_job(
                            self._adapter.lookup_station, name  # type: ignore[union-attr]
                        )
                    except Exception as exc:
                        _LOGGER.error("Options end stop lookup error: %s", exc, exc_info=True)
                        results = []
                        errors["base"] = "cannot_connect"

                    if not errors:
                        if not results:
                            errors["base"] = "station_not_found"
                        elif len(results) == 1:
                            self._end_name = results[0].get("name") or name
                            self._end_gid  = _gid(results[0]) or ""
                        else:
                            self._stop_candidates = results
                            self._stop_picker_for = "end"
                            return await self.async_step_pick_stop()
            else:
                self._end_name = self._end_gid = ""

            if not errors:
                return await self._fetch_lines_and_advance()

        return self.async_show_form(
            step_id="end_stop",
            data_schema=vol.Schema({
                vol.Optional(CONF_END_STOP_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            description_placeholders={
                "start": self._start_name,
                "example": "Frölunda Torg",
            },
            errors=errors,
        )

    # ── Shared stop picker ────────────────────────────────────────────────────

    async def async_step_pick_stop(self, user_input: dict | None = None) -> dict:
        if user_input is not None:
            chosen_gid = user_input.get("picked_stop", "")
            chosen = next(
                (r for r in self._stop_candidates if _gid(r) == chosen_gid),
                self._stop_candidates[0],
            )
            if self._stop_picker_for == "start":
                self._start_name = chosen.get("name") or ""
                self._start_gid  = _gid(chosen) or ""
                return await self.async_step_end_stop()
            else:
                self._end_name = chosen.get("name") or ""
                self._end_gid  = _gid(chosen) or ""
                return await self._fetch_lines_and_advance()

        options = [
            {"value": _gid(r) or str(i), "label": _stop_label(r)}
            for i, r in enumerate(self._stop_candidates)
        ]
        return self.async_show_form(
            step_id="pick_stop",
            data_schema=vol.Schema({
                vol.Required("picked_stop"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
        )

    async def _fetch_lines_and_advance(self) -> dict:
        stop_gid = self._start_gid

        def _do_fetch() -> list[dict]:
            # A full day at the default 2 per line+direction lists every line
            # serving the stop, including ones not running in the next hour.
            return self._adapter.get_departures(  # type: ignore[union-attr]
                stop_gid, limit=60, time_span_minutes=1440, max_pages=4
            )

        try:
            self._live_departures = await self.hass.async_add_executor_job(_do_fetch)
        except Exception as exc:
            _LOGGER.error(
                "Options: departure fetch failed for %s: %s", self._start_name, exc, exc_info=True
            )
            self._live_departures = []

        if self._end_gid and self._live_departures:
            start_gid = self._start_gid
            end_gid   = self._end_gid

            try:
                self._journey_line_dirs = await self.hass.async_add_executor_job(
                    self._adapter.line_directions, start_gid, end_gid  # type: ignore[union-attr]
                )
                journey_lines = set(self._journey_line_dirs)
                all_lines = _lines_from_departures(self._live_departures)
                filtered  = [l for l in all_lines if l["short_name"] in journey_lines]
                self._available_lines = filtered if filtered else all_lines
            except Exception as exc:
                _LOGGER.warning("Options journey plan failed: %s", exc)
                self._available_lines = _lines_from_departures(self._live_departures)
        else:
            self._available_lines = _lines_from_departures(self._live_departures)

        if not self._available_lines:
            return await self.async_step_line_manual()
        return await self.async_step_pick_line()

    # ── Pick line ─────────────────────────────────────────────────────────────

    async def async_step_pick_line(self, user_input: dict | None = None) -> dict:
        if user_input is not None:
            short = (user_input.get("line") or "").strip()
            match = next((l for l in self._available_lines if l["short_name"] == short), None)
            self._line_name = short
            self._line_gid  = (match or {}).get("gid") or ""
            self._line_mode = (match or {}).get("transport_mode") or "bus"

            if self._end_name:
                # The journey plan knows which way reaches the end stop; a
                # headsign match is only the fallback.
                planned = self._journey_line_dirs.get(short, "")
                if not planned:
                    dirs = _directions_for_line(self._live_departures, short)
                    end_lower = self._end_name.lower()
                    planned = next(
                        (d["direction"] for d in dirs if end_lower in d["direction"].lower()), ""
                    )
                if planned:
                    self._direction     = planned
                    self._direction_gid = await self._resolve_direction_gid(short, planned)
                    return await self.async_step_line_options()

            return await self.async_step_pick_direction()

        options = [{"value": l["short_name"], "label": l["label"]} for l in self._available_lines]
        return self.async_show_form(
            step_id="pick_line",
            data_schema=vol.Schema({
                vol.Required("line"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
            description_placeholders={"stop": self._start_name},
        )

    async def async_step_line_manual(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_LINE_NAME) or "").strip()
            if not name:
                errors["base"] = "line_required"
            else:
                self._line_name = name
                self._line_gid  = ""
                self._line_mode = "bus"
                return await self.async_step_pick_direction()

        return self.async_show_form(
            step_id="line_manual",
            data_schema=vol.Schema({
                vol.Required(CONF_LINE_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            description_placeholders={"stop": self._start_name},
            errors=errors,
        )

    async def async_step_pick_direction(self, user_input: dict | None = None) -> dict:
        if user_input is not None:
            chosen = (user_input.get("direction") or "").strip()
            if chosen == "__any__" or not chosen:
                self._direction = self._direction_gid = ""
            else:
                dirs  = _directions_for_line(self._live_departures, self._line_name)
                match = next((d for d in dirs if d["direction"] == chosen), None)
                self._direction     = chosen
                self._direction_gid = (match or {}).get("direction_gid") or ""
                if not self._direction_gid:
                    self._direction_gid = await self._resolve_direction_gid(
                        self._line_name, chosen
                    )
            return await self.async_step_line_options()

        dirs    = _directions_for_line(self._live_departures, self._line_name)
        options = [{"value": "__any__", "label": "Any direction"}]
        options += [{"value": d["direction"], "label": d["label"]} for d in dirs]
        return self.async_show_form(
            step_id="pick_direction",
            data_schema=vol.Schema({
                vol.Required("direction", default="__any__"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
            description_placeholders={"line": self._line_name, "stop": self._start_name},
        )

    async def async_step_line_options(self, user_input: dict | None = None) -> dict:
        default_name = f"{self._line_name} – {self._start_name}"
        if self._end_name:
            default_name += f" → {self._end_name}"
        elif self._direction:
            default_name += f" → {self._direction}"

        if user_input is not None:
            entry: dict = {
                CONF_STOP_NAME:      self._start_name,
                CONF_STOP_GID:       self._start_gid,
                CONF_LINE_NAME:      self._line_name,
                CONF_TRANSPORT_MODE: self._line_mode,
                CONF_DELAY:          int(user_input.get(CONF_DELAY, DEFAULT_DELAY)),
                CONF_NAME:           (user_input.get(CONF_NAME) or default_name).strip(),
            }
            if self._line_gid:
                entry[CONF_LINE_GID] = self._line_gid
            if self._end_name:
                entry[CONF_END_STOP_NAME] = self._end_name
                entry[CONF_END_STOP_GID]  = self._end_gid
            if self._direction:
                entry[CONF_DIRECTION]     = self._direction
                entry[CONF_DIRECTION_GID] = self._direction_gid
            if any(line_key(m) == line_key(entry) for m in self._monitored):
                return self._show_line_options(default_name, {"base": "line_exists"})
            self._monitored.append(entry)

            if user_input.get("add_another"):
                self._reset()
                return await self.async_step_start_stop()
            return self._save()

        return self._show_line_options(default_name)

    def _show_line_options(self, default_name: str, errors: dict | None = None) -> dict:
        direction_label = self._direction or self._end_name or "any direction"
        return self.async_show_form(
            step_id="line_options",
            errors=errors or {},
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_DELAY, default=DEFAULT_DELAY if self._walk is None else self._walk
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=30, step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(CONF_NAME, default=default_name): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional("add_another", default=False): BooleanSelector(),
            }),
            description_placeholders={
                "line":      self._line_name,
                "stop":      self._start_name,
                "direction": direction_label,
            },
        )

    # ── Edit ──────────────────────────────────────────────────────────────────

    async def async_step_edit(self, user_input: dict | None = None) -> dict:
        """Pick the line to change. Walk time and name are not part of a line's
        identity, so its entities keep their IDs."""
        if not self._monitored:
            return self.async_abort(reason="no_lines")
        if user_input is not None or len(self._monitored) == 1:
            self._edit_index = int((user_input or {}).get("line", 0))
            return await self.async_step_edit_line()
        options = [
            {"value": str(i), "label": m.get(CONF_NAME) or f"{m.get(CONF_LINE_NAME)} – {m.get(CONF_STOP_NAME)}"}
            for i, m in enumerate(self._monitored)
        ]
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema({
                vol.Required("line"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
            }),
        )

    async def async_step_edit_line(self, user_input: dict | None = None) -> dict:
        line = self._monitored[self._edit_index]
        if user_input is not None:
            self._monitored[self._edit_index] = {
                **line,
                CONF_DELAY: int(user_input.get(CONF_DELAY, DEFAULT_DELAY)),
                CONF_NAME: (user_input.get(CONF_NAME) or line.get(CONF_NAME) or "").strip(),
            }
            return self._save()
        return self.async_show_form(
            step_id="edit_line",
            data_schema=vol.Schema({
                vol.Optional(CONF_DELAY, default=line.get(CONF_DELAY, DEFAULT_DELAY)): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=30, step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(CONF_NAME, default=line.get(CONF_NAME, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            description_placeholders={
                "line": str(line.get(CONF_LINE_NAME)),
                "stop": str(line.get(CONF_STOP_NAME)),
            },
        )

    # ── Remove ────────────────────────────────────────────────────────────────

    async def async_step_remove(self, user_input: dict | None = None) -> dict:
        if not self._monitored:
            return self.async_abort(reason="no_lines")
        if user_input is not None:
            keep = set(user_input.get("keep", []))
            self._monitored = [m for i, m in enumerate(self._monitored) if str(i) in keep]
            return self._save()
        choices = {
            str(i): (
                m.get(CONF_NAME)
                or f"{m.get(CONF_LINE_NAME)} – {m.get(CONF_STOP_NAME)}"
            )
            for i, m in enumerate(self._monitored)
        }
        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema({
                vol.Required("keep", default=list(choices.keys())): cv.multi_select(choices),
            }),
        )

    def _save(self) -> dict:
        changed = self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                CONF_KEY:             self._key,
                CONF_SECRET:          self._secret,
                CONF_LANGUAGE:        self._language,
                CONF_USE_HOME:        self._use_home,
                **self._apis,
                CONF_MONITORED_LINES: self._monitored,
            },
        )
        if changed:
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
        return self.async_create_entry(title="", data={})

