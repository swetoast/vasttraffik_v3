"""Data coordinators.

Line   — departures of one monitored line at its stop, already narrowed to the
         trips that go the configured way.
Route  — journeys between a stop pair on any line, for the trip sensor.
Parking — commuter parking tied to the monitored boarding stops.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as ha_now
from homeassistant.util.location import distance

from ._helpers import (
    best_departure_dt,
    boarding_index,
    destination_call,
    hhmm,
    line_key,
    parse_dt,
    parse_full_forecast,
    parse_journey,
    parse_parking_area,
    parse_trip_ticket,
    short_direction,
    situation_scope,
)
from .api import VtjpAdapter
from .const import (
    CONF_DELAY,
    CONF_DIRECTION,
    CONF_END_STOP_GID,
    CONF_LINE_NAME,
    CONF_MONITORED_LINES,
    CONF_STOP_GID,
    DEFAULT_DELAY,
    DEPARTURE_SCAN_INTERVAL,
    PARKING_SCAN_INTERVAL,
    SEVERITY_ORDER,
)

_LOGGER = logging.getLogger(__name__)

LOOKBACK = timedelta(minutes=2)         # keeps a vehicle that is just leaving in view
FUTURE_HORIZON = timedelta(minutes=60)  # beyond the walk-time delay
MAX_NEW_CLASSIFICATIONS = 3             # detail lookups spent per refresh on unknown trips
JOURNEY_CACHE_SIZE = 12
FORECAST_INTERVAL = timedelta(minutes=30)
SKIP_RECHECK = timedelta(minutes=10)    # how often a part-cancelled trip is looked at again
TICKET_INTERVAL = timedelta(hours=6)
MAX_ALERTS = 20
HOME_RADIUS_M = 1500  # a stop this close to home is reached on foot
DOOR_TO_DOOR_STRIKES = 3  # empty door-to-door searches in a row before giving up on it


class VasttrafikDepartureCoordinator(DataUpdateCoordinator[dict]):
    """data = {
      "departures":        every departure from the stop in the window,
      "matched":           this line's departures going the right way, by time,
      "direction_matched": False when nothing matched and "matched" is every direction,
      "next":              first of "matched" that can still be caught after the walk,
      "next_arrival":      arrival at the end stop for "next", when one is configured,
      "tracked":           the departure the vehicle tracker follows,
      "cancelled":         upcoming trips this way that are cancelled or skip your stop,
    }

    `alerts` is a short log of things that changed for upcoming trips (platform,
    cancellation, a skipped stop, a disruption); the event entity replays it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: VtjpAdapter,
        ml: dict,
        idx: int,
    ) -> None:
        self._api = api
        self._idx = idx
        self.ml = ml
        self.delay = timedelta(minutes=ml.get(CONF_DELAY, DEFAULT_DELAY))
        # (headsign, stop point) → does a trip in that group reach the end stop?
        # Learned from one detail lookup per group, then reused for every trip.
        self._reaches: dict[tuple[str, str], bool] = {}
        self._journeys: dict[str, tuple[list[dict], list[dict]]] = {}
        self._tracked_ref: str | None = None
        # Last seen state per upcoming trip, to notice when something changes.
        self._seen: dict[str, dict] = {}
        self._primed = False  # the first refresh is the baseline, not news
        self._moves: set[tuple[str, str]] = set()  # platform moves in effect last refresh
        self._skipped: set[str] = set()
        self._skip_checked: dict[str, datetime] = {}
        self.alerts: list[dict] = []
        self._alert_seq = 0
        # Active traffic situations for the line; kept current by the Störning sensor.
        self.situations: list[dict] = []

        stop = ml.get(CONF_STOP_GID, "")
        line = ml.get(CONF_LINE_NAME, "")
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"vasttrafik departures[{idx}] {line}@{stop}",
            update_interval=DEPARTURE_SCAN_INTERVAL,
        )

    # ── Refresh ────────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        stop_gid = self.ml[CONF_STOP_GID]
        now = ha_now()
        fetch_from = now - LOOKBACK
        span_minutes = int((self.delay + LOOKBACK + FUTURE_HORIZON).total_seconds() // 60)

        # No server-side directionGid: it pins to one terminus, which drops
        # trips of the same travel direction that end elsewhere.
        def _fetch() -> list[dict]:
            return self._api.get_departure_window(
                stop_gid, when=fetch_from, time_span_minutes=span_minutes
            )

        try:
            departures = await self.hass.async_add_executor_job(_fetch)
        except ConfigEntryAuthFailed:
            raise  # lets Home Assistant start the reauth flow
        except Exception as exc:
            raise UpdateFailed(f"Departure fetch failed: {exc}") from exc

        line_name = self.ml.get(CONF_LINE_NAME, "")
        mine: list[tuple[datetime, dict]] = []
        for dep in departures:
            line = (dep.get("serviceJourney") or {}).get("line") or {}
            t = best_departure_dt(dep)
            if (line.get("shortName") or "") == line_name and t:
                mine.append((t, dep))
        mine.sort(key=lambda item: item[0])

        this_way, direction_matched = await self._match_direction(mine, now)
        await self._watch(this_way, now)

        def off(dep: dict) -> bool:
            return bool(dep.get("isCancelled")) or dep.get("detailsReference") in self._skipped

        matched = [dep for dep in this_way if not off(dep)]
        cancelled = [
            dep for dep in this_way if off(dep) and (best_departure_dt(dep) or now) >= now
        ]
        target = now + self.delay
        upcoming = [dep for dep in matched if (best_departure_dt(dep) or now) >= target]
        nxt = upcoming[0] if upcoming else None

        result: dict = {
            "departures": departures,
            "matched": matched,
            "direction_matched": direction_matched,
            "next": nxt,
            "next_arrival": None,
            "tracked": self._pick_tracked(matched, nxt, now),
            "cancelled": cancelled,
        }
        if nxt is not None and self.ml.get(CONF_END_STOP_GID):
            result["next_arrival"] = await self._destination_arrival(nxt)
        return result

    def upcoming(self) -> list[dict]:
        """Matched departures that can still be caught after the walk, as of now."""
        target = ha_now() + self.delay
        return [
            dep for dep in (self.data or {}).get("matched") or []
            if (best_departure_dt(dep) or target) >= target
        ]

    # ── Direction ──────────────────────────────────────────────────────────────

    @staticmethod
    def _group(dep: dict) -> tuple[str, str]:
        headsign = (dep.get("serviceJourney") or {}).get("direction")
        return short_direction(headsign), (dep.get("stopPoint") or {}).get("gid") or ""

    async def _match_direction(
        self, mine: list[tuple[datetime, dict]], now: datetime
    ) -> tuple[list[dict], bool]:
        """With an end stop, a trip matches if it actually calls there after this
        stop. Without one, or until a group is learned, the stored headsign decides."""
        want = short_direction(self.ml.get(CONF_DIRECTION)) or None
        end_gid = self.ml.get(CONF_END_STOP_GID)

        def headsign_ok(dep: dict) -> bool:
            headsign = (dep.get("serviceJourney") or {}).get("direction")
            return not want or not headsign or short_direction(headsign) == want

        if end_gid:
            budget = MAX_NEW_CLASSIFICATIONS
            for t, dep in mine:
                key = self._group(dep)
                if key in self._reaches or budget <= 0 or t < now or dep.get("isCancelled"):
                    continue
                budget -= 1
                journey = await self.async_journey(dep)
                if journey is not None:
                    self._reaches[key] = (
                        destination_call(journey[1], self.ml[CONF_STOP_GID], end_gid) is not None
                    )
            self._heal_stored_direction(mine)

        def ok(dep: dict) -> bool:
            learned = self._reaches.get(self._group(dep)) if end_gid else None
            return headsign_ok(dep) if learned is None else learned

        matched = [dep for _, dep in mine if ok(dep)]
        if matched:
            return matched, True
        return [dep for _, dep in mine], False

    def _heal_stored_direction(self, mine: list[tuple[datetime, dict]]) -> None:
        """Rewrite a stored headsign that turned out to point the wrong way, so
        the entry describes what is really monitored."""
        good = {key[0] for key, reaches in self._reaches.items() if reaches}
        stored = short_direction(self.ml.get(CONF_DIRECTION))
        if not good or not stored or stored in good:
            return
        headsign = next(
            ((dep.get("serviceJourney") or {}).get("direction") for _, dep in mine
             if self._reaches.get(self._group(dep))),
            None,
        )
        entry = self.config_entry
        lines = list(entry.data.get(CONF_MONITORED_LINES, [])) if entry else []
        if not headsign or self._idx >= len(lines) or line_key(lines[self._idx]) != line_key(self.ml):
            return
        _LOGGER.info(
            "Line %s from %s: stored direction %r does not reach the end stop; using %r",
            self.ml.get(CONF_LINE_NAME), self.ml.get(CONF_STOP_GID),
            self.ml.get(CONF_DIRECTION), headsign,
        )
        self.ml = {**self.ml, CONF_DIRECTION: headsign}
        lines[self._idx] = self.ml
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MONITORED_LINES: lines}
        )

    # ── Changes to upcoming trips ──────────────────────────────────────────────

    def push_alert(self, kind: str, **data: object) -> None:
        self._alert_seq += 1
        self.alerts.append({"seq": self._alert_seq, "type": kind, **data})
        del self.alerts[:-MAX_ALERTS]

    async def _watch(self, this_way: list[dict], now: datetime) -> None:
        """Compare every upcoming trip with how it looked last time."""
        current: dict[str, dict] = {}
        moves: set[tuple[str, str]] = set()
        announced: set[tuple[str, str]] = set()
        for dep in this_way:
            t, ref = best_departure_dt(dep), dep.get("detailsReference")
            if not ref or t is None or t < now:
                continue
            planned_stop = dep.get("stopPoint") or {}
            platform = (dep.get("realtimeStopPoint") or planned_stop).get("platform")
            previous = self._seen.get(ref)
            # Unseen trips are compared with the timetable, so a stop that was
            # already moved when the trip came into view still counts.
            before = previous["platform"] if previous else planned_stop.get("platform")
            state = {
                "platform": platform,
                "cancelled": bool(dep.get("isCancelled")),
                "skipped": ref in self._skipped,
            }
            about = {
                "departure": hhmm(t),
                "line": self.ml.get(CONF_LINE_NAME),
                "direction": (dep.get("serviceJourney") or {}).get("direction"),
            }
            if dep.get("isPartCancelled") and not state["cancelled"] and not state["skipped"]:
                skipped_stop = await self._skipped_stop(dep, now)
                if skipped_stop:
                    self._skipped.add(ref)
                    state["skipped"] = True
                    if self._primed:
                        self.push_alert("stop_skipped", **about, stop=skipped_stop)
            timetable = planned_stop.get("platform")
            if platform and timetable and platform != timetable:
                moves.add((timetable, platform))
            if self._primed:
                # A stop relocated for weeks moves every trip the same way: say
                # so once, not again for each bus that comes into view.
                move = (before, platform)
                if (
                    platform and before and platform != before
                    and move not in self._moves and move not in announced
                ):
                    self.push_alert(
                        "platform_changed", **about, from_platform=before, to_platform=platform
                    )
                    announced.add(move)
                if state["cancelled"] and not (previous or {}).get("cancelled"):
                    self.push_alert("departure_cancelled", **about)
            current[ref] = state
        self._moves = moves
        self._seen = current
        self._skipped &= set(current)
        self._skip_checked = {r: at for r, at in self._skip_checked.items() if r in current}
        self._primed = True

    async def _skipped_stop(self, dep: dict, now: datetime) -> str | None:
        """For a partly cancelled trip: "boarding" or "destination" if the part
        that is cancelled is one of your two stops. Needs fresh stop calls, so it
        is rate-limited per trip."""
        ref = dep["detailsReference"]
        checked = self._skip_checked.get(ref)
        if checked and now - checked < SKIP_RECHECK:
            return None
        self._skip_checked[ref] = now
        journey = await self.async_journey(dep, refresh=True)
        if journey is None:
            return None
        calls = journey[1]
        board = boarding_index(calls, self.ml[CONF_STOP_GID])
        if board is not None and (
            calls[board].get("isCancelled") or calls[board].get("isDepartureCancelled")
        ):
            return "boarding"
        end_gid = self.ml.get(CONF_END_STOP_GID)
        end = destination_call(calls, self.ml[CONF_STOP_GID], end_gid) if end_gid else None
        if end and (end.get("isCancelled") or end.get("isArrivalCancelled")):
            return "destination"
        return None

    # ── Traffic situations ─────────────────────────────────────────────────────

    def situation_scope(self, situation: dict) -> str:
        stops = {self.ml.get(CONF_STOP_GID), self.ml.get(CONF_END_STOP_GID)} - {None, ""}
        journeys = {
            (dep.get("serviceJourney") or {}).get("gid") for dep in self.upcoming()[:3]
        } - {None}
        return situation_scope(situation, stops, journeys)

    def relevant_disruption(self) -> dict | None:
        """The worst active situation that touches your stops or your next buses,
        as opposed to something elsewhere on the line."""
        close = [
            (s, scope) for s in self.situations
            if (scope := self.situation_scope(s)) in ("trip", "stop")
        ]
        if not close:
            return None

        def rank(item: tuple[dict, str]) -> tuple[int, int]:
            severity = (item[0].get("severity") or "").upper()
            return (
                item[1] == "trip",
                SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 0,
            )

        situation, scope = max(close, key=rank)
        return {"title": situation.get("title"), "scope": scope, "severity": situation.get("severity")}

    # ── Tracked vehicle ────────────────────────────────────────────────────────

    def _pick_tracked(
        self, matched: list[dict], nxt: dict | None, now: datetime
    ) -> dict | None:
        """Stay on the bus the sensor pointed at until it has left the stop: once
        you start walking it stops being "catchable", but it is the one to watch."""
        if self._tracked_ref:
            for dep in matched:
                t = best_departure_dt(dep)
                if dep.get("detailsReference") == self._tracked_ref and t and t >= now - LOOKBACK:
                    return dep
        self._tracked_ref = nxt.get("detailsReference") if nxt else None
        return nxt

    # ── Journey details ────────────────────────────────────────────────────────

    async def async_journey(
        self, dep: dict, *, refresh: bool = False
    ) -> tuple[list[dict], list[dict]] | None:
        """(path coordinates, stop calls) of a departure's trip. The route is
        fixed for the life of the trip, so each is fetched once and shared by
        direction matching, the arrival estimate and the tracker. *refresh*
        fetches again, for the per-stop cancellation flags."""
        ref = dep.get("detailsReference")
        if not ref:
            return None
        if ref in self._journeys and not refresh:
            return self._journeys[ref]

        def _fetch() -> dict:
            return self._api.get_departure_details(
                self.ml[CONF_STOP_GID], ref,
                includes=["servicejourneycalls", "servicejourneycoordinates"],
            )

        try:
            data = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Departure details failed for %s: %s", ref[:16], exc)
            return None
        journeys = data.get("serviceJourneys") or []
        first = (journeys[0] or {}) if journeys else {}
        calls = first.get("callsOnServiceJourney") or []
        if not calls:
            return None
        self._journeys[ref] = (first.get("serviceJourneyCoordinates") or [], calls)
        while len(self._journeys) > JOURNEY_CACHE_SIZE:
            self._journeys.pop(next(iter(self._journeys)))
        return self._journeys[ref]

    async def _destination_arrival(self, dep: dict) -> dict | None:
        journey = await self.async_journey(dep)
        if journey is None:
            return None
        calls = journey[1]
        call = destination_call(calls, self.ml[CONF_STOP_GID], self.ml[CONF_END_STOP_GID])
        arrival = parse_dt(
            (call or {}).get("estimatedOtherwisePlannedArrivalTime")
            or (call or {}).get("plannedArrivalTime")
        )
        departure = best_departure_dt(dep)
        # The stop calls were fetched once and do not follow later delays, but
        # the ride time between the two stops holds: add it to the live departure.
        board = boarding_index(calls, self.ml[CONF_STOP_GID])
        left = parse_dt(
            (calls[board].get("estimatedOtherwisePlannedDepartureTime")
             or calls[board].get("plannedDepartureTime")) if board is not None else None
        )
        if arrival and left and departure and arrival >= left:
            arrival = departure + (arrival - left)
        if arrival is None or (departure and arrival < departure):
            return None
        return {
            "details_reference": dep.get("detailsReference"),
            "arrival_time": arrival.isoformat(),
            "arrival_hhmm": hhmm(arrival),
            "duration_minutes": (
                int((arrival - departure).total_seconds() // 60) if departure else None
            ),
        }


class VasttrafikRouteCoordinator(DataUpdateCoordinator[list[dict]]):
    """Journeys from one stop to another on any line, earliest first.

    When Home Assistant's home is beside one end of the pair, that end is
    searched from the home coordinate instead, so Västtrafik supplies the walk:
    out the door at one end, or in the door at the other. If the boarding stop
    is too far to walk but has commuter parking, the drive there is planned.
    Which end is at home is worked out from the stop coordinates in the first
    stop-to-stop result.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: VtjpAdapter,
        origin: tuple[str, str],
        destination: tuple[str, str],
        delay: timedelta,
        *,
        home: tuple[float, float] | None = None,
        origin_has_parking: bool = False,
    ) -> None:
        self._api = api
        self.origin_gid, self.origin_name = origin
        self.destination_gid, self.destination_name = destination
        self.delay = delay
        self._home = home
        # "origin" / "destination": which end of the pair is at home, if either.
        self.home_end: str | None = None
        self._drive = False
        self._origin_has_parking = origin_has_parking
        self._home_located = home is None
        self._door_strikes = 0
        self.ticket: dict | None = None
        self._ticket_at: datetime | None = None
        self._access_minutes = 0
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"vasttrafik route {self.origin_gid}→{self.destination_gid}",
            update_interval=DEPARTURE_SCAN_INTERVAL,
        )

    @property
    def from_home(self) -> bool:
        return self.home_end == "origin" or self._drive

    async def _async_update_data(self) -> list[dict]:
        now = ha_now()
        try:
            plain: list[dict] | None = None
            if not self._home_located:
                plain = await self._search(now, door_to_door=False)
                self._locate_home(plain)
            if self.home_end or self._drive:
                journeys = await self._search(now, door_to_door=True)
                if journeys:
                    self._door_strikes = 0
                else:
                    # Nothing usable from the coordinate search: use the stops,
                    # and stop trying if this keeps happening or it was refused.
                    self._door_strikes += 1
                    if self._door_strikes >= DOOR_TO_DOOR_STRIKES:
                        _LOGGER.debug("%s: door-to-door search dropped", self.name)
                        self.home_end, self._drive = None, False
                    journeys = plain if plain is not None else await self._search(
                        now, door_to_door=False
                    )
            else:
                journeys = plain if plain is not None else await self._search(
                    now, door_to_door=False
                )
        except ConfigEntryAuthFailed:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Journey search failed: {exc}") from exc

        access = [j["access"]["minutes"] for j in journeys if j["access"] and j["access"]["minutes"]]
        self._access_minutes = min(access) if access else 0
        await self._refresh_ticket(journeys, now)
        return journeys

    def _locate_home(self, journeys: list[dict]) -> None:
        def metres(position: tuple) -> float | None:
            if not self._home or None in position:
                return None
            return distance(self._home[0], self._home[1], position[0], position[1])

        sample = next((j for j in journeys if None not in j["board_pos"] + j["alight_pos"]), None)
        if sample is None:
            return  # no coordinates yet; try again on the next refresh
        self._home_located = True
        to_origin, to_destination = metres(sample["board_pos"]), metres(sample["alight_pos"])
        if to_origin is not None and to_origin <= HOME_RADIUS_M:
            self.home_end = "origin"
        elif to_destination is not None and to_destination <= HOME_RADIUS_M:
            self.home_end = "destination"
        elif self._origin_has_parking:
            self._drive = True

    async def _search(self, now: datetime, *, door_to_door: bool) -> list[dict]:
        from_home = door_to_door and self.from_home
        to_home = door_to_door and self.home_end == "destination"
        # The search time is when you leave the origin: the door when starting
        # from home, otherwise the stop after the configured walk.
        lead = self.delay - timedelta(minutes=self._access_minutes) if from_home else self.delay
        when = now + max(lead, timedelta(0))

        def _fetch() -> dict:
            return self._api.plan_journey(
                self.origin_gid, self.destination_gid,
                when=when, limit=8, include_occupancy=True,
                origin_coords=self._home if from_home else None,
                destination_coords=self._home if to_home else None,
                origin_park=door_to_door and self._drive,
            )

        try:
            plan = await self.hass.async_add_executor_job(_fetch)
        except Exception as exc:
            if not (from_home or to_home):
                raise
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 400:
                self._door_strikes = DOOR_TO_DOOR_STRIKES  # refused outright: do not retry
            return []
        journeys = [j for j in map(parse_journey, plan.get("results") or []) if j]
        # The pair is what was asked for. From a coordinate the planner is free
        # to use any stop nearby, so only trips through the pair's own stop count.
        if from_home and not self._drive:
            journeys = [j for j in journeys if j["board_gid"] == self.origin_gid]
        if to_home:
            journeys = [j for j in journeys if j["alight_gid"] == self.destination_gid]
        return sorted(journeys, key=lambda j: j["departure"])

    async def _refresh_ticket(self, journeys: list[dict], now: datetime) -> None:
        """The ticket for a stop pair rarely changes, so it is asked for a few
        times a day rather than with every search."""
        if self._ticket_at and now - self._ticket_at < TICKET_INTERVAL:
            return
        ref = next((j["details_reference"] for j in journeys if j["details_reference"]), None)
        if not ref:
            return
        self._ticket_at = now
        try:
            details = await self.hass.async_add_executor_job(
                self._api.journey_details, ref, ["ticketsuggestions", "validzones"]
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Ticket suggestion failed for %s: %s", self.name, exc)
            return
        self.ticket = parse_trip_ticket(details) or self.ticket

    def leave_time(self, journey: dict) -> datetime:
        """When to walk out: the configured walk time before departure, or, with
        none configured, the start of Västtrafik's own access leg."""
        if self.delay:
            return journey["departure"] - self.delay
        access = journey["access"]
        return (access or {}).get("time") or journey["departure"]

    def upcoming(self) -> list[dict]:
        now = ha_now()
        return [
            j for j in self.data or []
            if self.leave_time(j) >= now and not j["is_cancelled"]
        ]


class VasttrafikParkingCoordinator(DataUpdateCoordinator[dict[int, dict]]):
    """Commuter parking areas linked to the boarding stops, keyed by area id.
    Empty when there are none or the application lacks the Pendelparkering API."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: VtjpAdapter, stop_gids: list[str]
    ) -> None:
        self._api = api
        self._stop_gids = stop_gids
        self._forecasts: dict[int, dict] = {}
        self._forecast_at: datetime | None = None
        self._details: dict[int, dict] = {}
        # stop gid → when the next trip leaves there; set once the routes exist.
        self.next_departure_from: Callable[[str], datetime | None] = lambda _gid: None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="vasttrafik commuter parking",
            update_interval=PARKING_SCAN_INTERVAL,
        )

    def reset_forecast(self) -> None:
        self._forecast_at = None

    async def _async_update_data(self) -> dict[int, dict]:
        try:
            return await self.hass.async_add_executor_job(self._fetch)
        except ConfigEntryAuthFailed:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Parking fetch failed: {exc}") from exc

    def _fetch(self) -> dict[int, dict]:
        now = ha_now()
        areas: dict[int, dict] = {}
        for stop_gid in self._stop_gids:
            for raw in self._api.parkings_for_stop(stop_gid) or []:
                area = parse_parking_area(raw, stop_gid)
                if area:
                    areas.setdefault(area["id"], area)

        refresh_forecast = (
            self._forecast_at is None or now - self._forecast_at >= FORECAST_INTERVAL
        )
        for area_id, area in areas.items():
            if area_id not in self._details:
                self._details[area_id] = self._static_details(area)
            if refresh_forecast and area["free"] is not None:
                self._forecasts[area_id] = self._forecast(area, now)
            area.update(self._forecasts.get(area_id) or {})
            area["details"] = self._details[area_id]
        if refresh_forecast and areas:
            self._forecast_at = now
        return areas

    def _forecast(self, area: dict, now: datetime) -> dict:
        """When it fills up today, and how many spaces to expect when you get
        there: at the time the next trip leaves the stop it belongs to."""
        text = self._api.parking_full_forecast(area["id"], now.strftime("%Y-%m-%d"))
        out: dict = {"full_at": parse_full_forecast(text, now)}
        departure = self.next_departure_from(area["stop_gid"])
        if departure and departure > now:
            out["free_at_departure"] = self._api.parking_forecast_free(area["id"], departure)
            out["forecast_for"] = hhmm(departure)
        return out

    def _static_details(self, area: dict) -> dict:
        """Facilities from Geografi. Its area number is assumed to equal the
        Pendelparkering id, so the data is only used when the names agree."""
        try:
            geo = self._api.commuter_parking_area(area["id"])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Parking details failed for %s: %s", area["id"], exc)
            return {}
        a, b = (area["name"] or "").casefold(), ((geo or {}).get("name") or "").casefold()
        if not geo or not a or not b or (a not in b and b not in a):
            return {}
        lots = geo.get("commuterParkingLots") or []
        return {
            "paid_parking":     sorted({l.get("paidParking") for l in lots if l.get("paidParking")}),
            "charging_station": any(l.get("hasChargingStation") for l in lots),
            "disabled_spaces":  any(l.get("hasDisabledParkingSpace") for l in lots),
            "bicycle_parking":  any(l.get("isBicycleParkingPossible") for l in lots),
        }
