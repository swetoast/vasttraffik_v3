"""
Config flow for Västtrafik v3.

Steps
──────
  1. user        — API credentials (key + secret)
  2. start_stop  — where you board
  3. end_stop    — where you get off (optional; filters the line list)
  4. pick_line   — choose from lines currently departing the start stop
  5. line_options — walk-time offset, name, "add another?" toggle

No separate direction step.  If an endpoint was given the direction is
derived automatically; otherwise all departures on the chosen line are shown.

Bug fixes vs previous version
──────────────────────────────
• Removed cross-step chaining (pick_line → pick_direction) which caused
  HA's flow runner to update cur_step after the request was already
  answered, so the following submit called the wrong handler.
• All API calls use named inner functions instead of lambdas so exceptions
  produce full tracebacks in the HA log.
• All transient per-iteration state lives in plain instance variables that
  are reset explicitly by _reset() — no hasattr() guards.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
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
    DOMAIN,
    SUPPORTED_LANGUAGES,
)
from .options import VasttrafikOptionsFlowHandler

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────── Shared pure helpers ──────────────────────────────

def _gid(loc: dict) -> str | None:
    return loc.get("gid") or loc.get("id") or loc.get("stopAreaGid")


def _stop_label(loc: dict) -> str:
    name = loc.get("name") or "Unknown stop"
    muni = loc.get("municipality") or ""
    return f"{name} – {muni}" if (muni and muni.lower() not in name.lower()) else name


def _natural_sort_key(s: str) -> tuple[int, str]:
    num   = "".join(c for c in s if c.isdigit())
    alpha = "".join(c for c in s if not c.isdigit())
    return (int(num) if num else 9999, alpha)


def _lines_from_departures(departures: list[dict]) -> list[dict]:
    """
    Return unique lines from a departure list, sorted naturally.
    Each item: {short_name, gid, transport_mode, label}
    """
    seen: dict[str, dict] = {}
    for dep in departures:
        try:
            sj    = dep.get("serviceJourney") or {}
            line  = sj.get("line") or {}
            short = (line.get("shortName") or line.get("name") or "").strip()
            if not short or short in seen:
                continue
            mode = (line.get("transportMode") or "bus").lower()
            icon = {"bus": "Bus", "tram": "Tram", "train": "Train",
                    "ferry": "Ferry"}.get(mode, "Bus")
            seen[short] = {
                "short_name": short,
                "gid": line.get("gid") or "",
                "transport_mode": mode,
                # Plain ASCII label avoids any emoji rendering issues in HA UI
                "label": f"{icon} {short}",
            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Skipping malformed departure entry: %s", exc)
    return sorted(seen.values(), key=lambda x: _natural_sort_key(x["short_name"]))


def _best_direction_for_endpoint(
    departures: list[dict], line_name: str, end_name: str
) -> tuple[str, str]:
    """
    Scan departures of *line_name* and return the (direction, direction_gid)
    whose label best matches *end_name*.  Returns ("", "") if no match.
    """
    end_lower = end_name.lower()
    best_dir = ""
    best_gid = ""
    for dep in departures:
        try:
            sj   = dep.get("serviceJourney") or {}
            line = sj.get("line") or {}
            if (line.get("shortName") or "") != line_name:
                continue
            direction = (
                sj.get("direction") or ""
            ).strip()
            if not direction:
                continue
            if end_lower in direction.lower():
                dest    = dep.get("destinationStopArea") or {}
                dir_gid = dest.get("gid") or ""
                return direction, dir_gid
            # Keep the first direction as fallback
            if not best_dir:
                dest    = dep.get("destinationStopArea") or {}
                best_dir = direction
                best_gid = dest.get("gid") or ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Skipping departure while scanning directions: %s", exc)
    return best_dir, best_gid


# ─────────────────────────── Config flow ─────────────────────────────────────

class VasttrafikConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Five-step setup:
      user → start_stop → (pick_stop?) → end_stop → (pick_stop?) → pick_line → line_options
    """

    VERSION = 3

    def __init__(self) -> None:
        self._key:      str = ""
        self._secret:   str = ""
        self._language: str = DEFAULT_LANGUAGE
        self._adapter:  VtjpAdapter | None = None
        self._monitored: list[dict] = []

        # Per-iteration transient state — reset by _reset()
        self._start_name: str = ""
        self._start_gid:  str = ""
        self._end_name:   str = ""
        self._end_gid:    str = ""

        self._stop_candidates: list[dict] = []
        self._stop_picker_for: str = ""    # "start" or "end"

        self._live_departures:  list[dict] = []
        self._available_lines:  list[dict] = []

        # Set after pick_line
        self._line_name:    str = ""
        self._line_gid:     str = ""
        self._line_mode:    str = ""
        self._direction:    str = ""
        self._direction_gid:str = ""

    def _reset(self) -> None:
        """Clear all per-line state before the next add-another loop."""
        self._start_name = self._start_gid = ""
        self._end_name   = self._end_gid   = ""
        self._stop_candidates  = []
        self._stop_picker_for  = ""
        self._live_departures  = []
        self._available_lines  = []
        self._line_name = self._line_gid = self._line_mode = ""
        self._direction = self._direction_gid = ""

    # ── Step 1: Credentials ───────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            key      = (user_input.get(CONF_KEY)      or "").strip()
            secret   = (user_input.get(CONF_SECRET)   or "").strip()
            language = (user_input.get(CONF_LANGUAGE) or DEFAULT_LANGUAGE)
            if not key or not secret:
                errors["base"] = "auth"
            else:
                try:
                    adapter = VtjpAdapter(key, secret, language=language)
                    await self.hass.async_add_executor_job(adapter.ensure_token)
                    self._key      = key
                    self._secret   = secret
                    self._language = language
                    self._adapter  = adapter
                except ConfigEntryAuthFailed:
                    _LOGGER.warning("Västtrafik config flow: auth failed")
                    errors["base"] = "auth"
                except Exception as exc:
                    _LOGGER.error(
                        "Västtrafik config flow: credential check failed: %s",
                        exc, exc_info=True,
                    )
                    errors["base"] = "cannot_connect"

            if not errors:
                return await self.async_step_start_stop()

        lang_options = [
            {"value": code, "label": label}
            for code, label in SUPPORTED_LANGUAGES.items()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_SECRET): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): SelectSelector(
                    SelectSelectorConfig(
                        options=lang_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    # ── Step 2: Start stop ────────────────────────────────────────────────────

    async def async_step_start_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_STOP_NAME) or "").strip()
            if not name:
                errors["base"] = "station_required"
            else:
                try:
                    def _lookup() -> list[dict]:
                        return self._adapter.lookup_station(name)  # type: ignore[union-attr]

                    results = await self.hass.async_add_executor_job(_lookup)
                    _LOGGER.debug(
                        "Start stop lookup %r → %d result(s)", name, len(results)
                    )
                except Exception as exc:
                    _LOGGER.error(
                        "Start stop lookup error for %r: %s", name, exc, exc_info=True
                    )
                    errors["base"] = "cannot_connect"
                    results = []

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

    # ── Step 3: End stop (optional) ───────────────────────────────────────────

    async def async_step_end_stop(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_END_STOP_NAME) or "").strip()

            if name:
                try:
                    def _lookup() -> list[dict]:
                        return self._adapter.lookup_station(name)  # type: ignore[union-attr]

                    results = await self.hass.async_add_executor_job(_lookup)
                    _LOGGER.debug(
                        "End stop lookup %r → %d result(s)", name, len(results)
                    )
                except Exception as exc:
                    _LOGGER.error(
                        "End stop lookup error for %r: %s", name, exc, exc_info=True
                    )
                    errors["base"] = "cannot_connect"
                    results = []

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
                # Blank → no endpoint; direction = any
                self._end_name = ""
                self._end_gid  = ""

            if not errors:
                return await self._fetch_lines_then_show_picker()

        return self.async_show_form(
            step_id="end_stop",
            data_schema=vol.Schema({
                vol.Optional(CONF_END_STOP_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            description_placeholders={
                "start":   self._start_name,
                "example": "Frölunda Torg",
            },
            errors=errors,
        )

    # ── Shared: stop disambiguation picker ────────────────────────────────────

    async def async_step_pick_stop(self, user_input: dict | None = None) -> dict:
        if user_input is not None:
            chosen_gid = user_input.get("picked_stop", "")
            chosen = next(
                (r for r in self._stop_candidates if _gid(r) == chosen_gid),
                self._stop_candidates[0] if self._stop_candidates else {},
            )
            if self._stop_picker_for == "start":
                self._start_name = chosen.get("name") or ""
                self._start_gid  = _gid(chosen) or ""
                return await self.async_step_end_stop()
            else:
                self._end_name = chosen.get("name") or ""
                self._end_gid  = _gid(chosen) or ""
                return await self._fetch_lines_then_show_picker()

        options = [
            {"value": _gid(r) or str(i), "label": _stop_label(r)}
            for i, r in enumerate(self._stop_candidates)
        ]
        return self.async_show_form(
            step_id="pick_stop",
            data_schema=vol.Schema({
                vol.Required("picked_stop"): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    # ── Fetch departures, then show pick_line ─────────────────────────────────

    async def _fetch_lines_then_show_picker(self) -> dict:
        """
        Fetch live departures from the start stop, optionally filter by
        journey-plan results for the endpoint, then return the pick_line form.

        Named function (not lambda) so any exception produces a traceback.
        """
        stop_gid = self._start_gid

        def _fetch_departures() -> list[dict]:
            return self._adapter.get_departures(stop_gid, limit=60)  # type: ignore[union-attr]

        try:
            self._live_departures = await self.hass.async_add_executor_job(
                _fetch_departures
            )
            _LOGGER.debug(
                "Fetched %d departures from %s (%s)",
                len(self._live_departures), self._start_name, self._start_gid,
            )
        except Exception as exc:
            _LOGGER.error(
                "Departure fetch failed for %s (%s): %s",
                self._start_name, self._start_gid, exc, exc_info=True,
            )
            self._live_departures = []

        # Filter to lines that serve the origin→destination pair when endpoint given
        if self._end_gid and self._live_departures:
            start_gid = self._start_gid
            end_gid   = self._end_gid

            def _plan_journey() -> dict:
                return self._adapter.plan_journey(start_gid, end_gid, limit=10)  # type: ignore[union-attr]

            try:
                plan = await self.hass.async_add_executor_job(_plan_journey)
                journey_lines: set[str] = set()
                for result in (plan.get("results") or []):
                    for leg in (result.get("tripLegs") or []):
                        short = (
                            (leg.get("serviceJourney") or {})
                            .get("line", {})
                            .get("shortName") or ""
                        )
                        if short:
                            journey_lines.add(short)

                all_lines = _lines_from_departures(self._live_departures)
                if journey_lines:
                    filtered = [l for l in all_lines if l["short_name"] in journey_lines]
                    self._available_lines = filtered if filtered else all_lines
                    _LOGGER.debug(
                        "Lines for %s→%s: %s",
                        self._start_name, self._end_name,
                        [l["short_name"] for l in self._available_lines],
                    )
                else:
                    self._available_lines = all_lines
            except Exception as exc:
                _LOGGER.warning(
                    "Journey plan failed (%s→%s): %s — showing all lines",
                    self._start_name, self._end_name, exc,
                )
                self._available_lines = _lines_from_departures(self._live_departures)
        else:
            self._available_lines = _lines_from_departures(self._live_departures)

        # Show pick_line form directly (no chaining to another step)
        return await self.async_step_pick_line()

    # ── Step 4: Pick line ─────────────────────────────────────────────────────

    async def async_step_pick_line(self, user_input: dict | None = None) -> dict:
        """
        Show all available lines as a selection list.
        On submit: resolve direction from endpoint (or leave as 'any'),
        then advance DIRECTLY to line_options without chaining through
        another step — this avoids the cur_step de-sync bug.
        """
        errors: dict = {}

        if user_input is not None:
            short = (user_input.get("line") or "").strip()
            if not short:
                errors["base"] = "line_required"
            else:
                try:
                    match = next(
                        (l for l in self._available_lines if l["short_name"] == short),
                        None,
                    )
                    self._line_name = short
                    self._line_gid  = (match or {}).get("gid") or ""
                    self._line_mode = (match or {}).get("transport_mode") or "bus"

                    # Derive direction from endpoint without a separate step
                    if self._end_name:
                        self._direction, self._direction_gid = (
                            _best_direction_for_endpoint(
                                self._live_departures, short, self._end_name
                            )
                        )
                        _LOGGER.debug(
                            "Auto-direction for line %s toward %s: %r",
                            short, self._end_name, self._direction,
                        )
                    else:
                        self._direction     = ""
                        self._direction_gid = ""

                except Exception as exc:
                    _LOGGER.error(
                        "pick_line processing failed for %r: %s", short, exc, exc_info=True
                    )
                    errors["base"] = "unknown"

            if not errors:
                return await self.async_step_line_options()

        # Build or rebuild the options list
        if not self._available_lines:
            # Edge case: no departures found — fall back to text entry
            return await self.async_step_line_manual()

        options = [
            {"value": l["short_name"], "label": l["label"]}
            for l in self._available_lines
        ]

        return self.async_show_form(
            step_id="pick_line",
            data_schema=vol.Schema({
                vol.Required("line"): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            description_placeholders={"stop": self._start_name},
            errors=errors,
        )

    # ── Fallback: type line name manually ─────────────────────────────────────

    async def async_step_line_manual(self, user_input: dict | None = None) -> dict:
        errors: dict = {}
        if user_input is not None:
            name = (user_input.get(CONF_LINE_NAME) or "").strip()
            if not name:
                errors["base"] = "line_required"
            else:
                self._line_name     = name
                self._line_gid      = ""
                self._line_mode     = "bus"
                self._direction     = ""
                self._direction_gid = ""
                return await self.async_step_line_options()

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

    # ── Step 5: Final options ─────────────────────────────────────────────────

    async def async_step_line_options(self, user_input: dict | None = None) -> dict:
        """
        Walk-time offset and name.
        All route state is already committed to self — no parameter passing needed.
        """
        default_name = f"{self._line_name} – {self._start_name}"
        if self._end_name:
            default_name += f" → {self._end_name}"
        elif self._direction:
            default_name += f" → {self._direction}"

        if user_input is not None:
            try:
                entry: dict = {
                    CONF_STOP_NAME:      self._start_name,
                    CONF_STOP_GID:       self._start_gid,
                    CONF_LINE_NAME:      self._line_name,
                    CONF_TRANSPORT_MODE: self._line_mode,
                    CONF_DELAY:          int(user_input.get(CONF_DELAY) or DEFAULT_DELAY),
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
                _LOGGER.debug("Appended monitored entry: %s", entry)
            except Exception as exc:
                _LOGGER.error(
                    "line_options save failed: %s", exc, exc_info=True
                )
                return self.async_show_form(
                    step_id="line_options",
                    data_schema=self._line_options_schema(default_name),
                    description_placeholders=self._line_options_placeholders(),
                    errors={"base": "unknown"},
                )

            if user_input.get("add_another"):
                self._reset()
                return await self.async_step_start_stop()

            return self._create_entry()

        return self.async_show_form(
            step_id="line_options",
            data_schema=self._line_options_schema(default_name),
            description_placeholders=self._line_options_placeholders(),
        )

    def _line_options_schema(self, default_name: str) -> vol.Schema:
        return vol.Schema({
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
        })

    def _line_options_placeholders(self) -> dict[str, str]:
        return {
            "line":      self._line_name,
            "stop":      self._start_name,
            "direction": self._direction or self._end_name or "any direction",
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _create_entry(self) -> dict:
        return self.async_create_entry(
            title="Västtrafik",
            data={
                CONF_KEY:             self._key,
                CONF_SECRET:          self._secret,
                CONF_LANGUAGE:        self._language,
                CONF_MONITORED_LINES: self._monitored,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return VasttrafikOptionsFlowHandler(config_entry)

