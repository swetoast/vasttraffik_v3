"""Options flow for Västtrafik v3 — add or remove monitored lines."""
from __future__ import annotations

import logging

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

from .api import VtjpAdapter
from .const import (
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
        icon = {"bus": "🚌", "tram": "🚃", "train": "🚆", "ferry": "⛴️"}.get(mode, "🚌")
        seen[short] = {
            "short_name": short,
            "gid": line.get("gid"),
            "transport_mode": mode,
            "label": f"{icon}  {short}",
        }
    return sorted(seen.values(), key=lambda x: _natural_sort_key(x["short_name"]))


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
        dest = dep.get("destinationStopArea") or {}
        seen[direction] = {
            "direction": direction,
            "direction_gid": dest.get("gid") or None,
            "label": f"→  {direction}",
        }
    return list(seen.values())


async def options_update_listener(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> None:
    """Reload the integration when config data changes."""
    await hass.config_entries.async_reload(entry.entry_id)


# ─────────────────────────── Options flow ────────────────────────────────────

class VasttrafikOptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._monitored: list[dict] = list(
            config_entry.data.get(CONF_MONITORED_LINES, [])
        )
        self._key:      str = config_entry.data.get(CONF_KEY,      "")
        self._secret:   str = config_entry.data.get(CONF_SECRET,   "")
        self._language: str = config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        self._adapter: VtjpAdapter | None = None

        # Per-iteration state — reset by _reset()
        self._start_name: str = ""
        self._start_gid:  str = ""
        self._end_name:   str = ""
        self._end_gid:    str = ""
        self._stop_candidates:  list[dict] = []
        self._stop_picker_for:  str = ""
        self._live_departures:  list[dict] = []
        self._available_lines:  list[dict] = []
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

    # ── Entry point ───────────────────────────────────────────────────────────

    async def async_step_init(self, user_input: dict | None = None) -> dict:
        return await self.async_step_menu()

    async def async_step_menu(self, user_input: dict | None = None) -> dict:
        if user_input:
            action = user_input.get("action", "")
            if action == "add":
                return await self.async_step_start_stop()
            if action == "remove":
                return await self.async_step_remove()
            if action == "language":
                return await self.async_step_language()
            return self._save()

        options = [{"value": "add", "label": "➕  Add a monitored line"}]
        if self._monitored:
            options.append({"value": "remove", "label": "🗑️  Remove a monitored line"})
        lang_label = SUPPORTED_LANGUAGES.get(self._language, self._language)
        options.append({"value": "language", "label": f"🌐  Language: {lang_label}"})
        options.append({"value": "save", "label": "✅  Save and close"})

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
        if user_input:
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

    # ── Add: start stop ───────────────────────────────────────────────────────

    async def async_step_start_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input:
            name = (user_input.get(CONF_STOP_NAME) or "").strip()
            if not name:
                errors["base"] = "station_required"
            elif not await self._ensure_adapter():
                errors["base"] = "cannot_connect"
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

        return self.async_show_form(
            step_id="start_stop",
            data_schema=vol.Schema({
                vol.Required(CONF_STOP_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            description_placeholders={"example": "Brunnsparken, Göteborg"},
            errors=errors,
        )

    # ── Add: end stop (optional) ──────────────────────────────────────────────

    async def async_step_end_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input:
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
        if user_input:
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
            return self._adapter.get_departures(  # type: ignore[union-attr]
                stop_gid, limit=60
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

            def _plan() -> dict:
                return self._adapter.plan_journey(  # type: ignore[union-attr]
                    start_gid, end_gid, limit=10
                )

            try:
                plan = await self.hass.async_add_executor_job(_plan)
                journey_lines: set[str] = set()
                for result in (plan.get("results") or []):
                    for leg in (result.get("tripLegs") or []):
                        short = ((leg.get("serviceJourney") or {}).get("line") or {}).get("shortName") or ""
                        if short:
                            journey_lines.add(short)
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
        if user_input:
            short = (user_input.get("line") or "").strip()
            match = next((l for l in self._available_lines if l["short_name"] == short), None)
            self._line_name = short
            self._line_gid  = (match or {}).get("gid") or ""
            self._line_mode = (match or {}).get("transport_mode") or "bus"

            if self._end_name:
                dirs = _directions_for_line(self._live_departures, short)
                end_lower = self._end_name.lower()
                best = next((d for d in dirs if end_lower in d["direction"].lower()), None)
                if best:
                    self._direction     = best["direction"]
                    self._direction_gid = best["direction_gid"] or ""
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
        if user_input:
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
        if user_input:
            chosen = (user_input.get("direction") or "").strip()
            if chosen == "__any__" or not chosen:
                self._direction = self._direction_gid = ""
            else:
                dirs  = _directions_for_line(self._live_departures, self._line_name)
                match = next((d for d in dirs if d["direction"] == chosen), None)
                self._direction     = chosen
                self._direction_gid = (match or {}).get("direction_gid") or ""
            return await self.async_step_line_options()

        dirs    = _directions_for_line(self._live_departures, self._line_name)
        options = [{"value": "__any__", "label": "🔀  Any direction"}]
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

        if user_input:
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
            self._monitored.append(entry)

            if user_input.get("add_another"):
                self._reset()
                return await self.async_step_start_stop()
            return self._save()

        direction_label = self._direction or self._end_name or "any direction"
        return self.async_show_form(
            step_id="line_options",
            data_schema=vol.Schema({
                vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): NumberSelector(
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

    # ── Remove ────────────────────────────────────────────────────────────────

    async def async_step_remove(self, user_input: dict | None = None) -> dict:
        if not self._monitored:
            return self.async_abort(reason="no_lines")
        if user_input:
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
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                CONF_KEY:             self._key,
                CONF_SECRET:          self._secret,
                CONF_LANGUAGE:        self._language,
                CONF_MONITORED_LINES: self._monitored,
            },
        )
        return self.async_create_entry(title="", data={})

