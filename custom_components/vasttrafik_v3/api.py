"""Adapter for the Västtrafik REST APIs.

Planera Resa v4 carries the integration. Störning v1, Geografi v3 and
Pendelparkering (SPP) v3 are extras: an application that is not subscribed
to one simply gets no data from its calls.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from homeassistant.exceptions import ConfigEntryAuthFailed

from ._helpers import journey_line_directions

_LOGGER = logging.getLogger(__name__)

BASE_URL     = "https://ext-api.vasttrafik.se/pr/v4"
STÖRNING_URL = "https://ext-api.vasttrafik.se/ts/v1"
GEO_URL      = "https://ext-api.vasttrafik.se/geo/v3"
SPP_URL      = "https://ext-api.vasttrafik.se/spp/v3"
TOKEN_URL    = "https://ext-api.vasttrafik.se/token"

# How long to leave an optional API alone after it refused this application.
OPTIONAL_API_RETRY_SECONDS = 6 * 3600
STÖRNING_RETRY_SECONDS = 3600

# The API returns only 2 departures per line and direction unless told otherwise,
# which is fewer than a look-back + walk-time window needs.
# The spec gives no upper bound, so smaller values are tried if one is refused.
PER_LINE_AND_DIRECTION_CANDIDATES: tuple[int | None, ...] = (40, 10, None)
DEPARTURE_PAGE_SIZE = 50
DEPARTURE_MAX_PAGES = 6
DEPARTURE_CACHE_SECONDS = 60


class VasttrafikConnectionError(Exception):
    """The API could not be reached or answered with a non-auth error."""


class VtjpAdapter:
    def __init__(self, key: str, secret: str, language: str = "sv") -> None:
        self._key    = key
        self._secret = secret
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Accept-Language": language if language in ("sv", "en") else "sv",
        })
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = threading.Lock()
        self._störning_unavailable: bool = False
        self._störning_warned: bool = False
        self._störning_retry_at: float = 0.0
        self._dep_lock = threading.Lock()
        self._dep_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
        self._per_line_caps = PER_LINE_AND_DIRECTION_CANDIDATES
        self._optional_blocked: dict[str, float] = {}
        self.disabled_apis: set[str] = set()  # switched off by the user: never called

    def close(self) -> None:
        self._session.close()

    @property
    def disruptions_unavailable(self) -> bool:
        return self._störning_unavailable

    # ── Auth ──────────────────────────────────────────────────────────────────

    def ensure_token(self) -> None:
        """Obtain/refresh the client-credentials token. Thread-safe: several
        executor threads share one Session and must not race on the header.

        Only a rejected key/secret raises ConfigEntryAuthFailed; an outage raises
        VasttrafikConnectionError so Home Assistant retries instead of asking
        the user to re-enter credentials that are still valid."""
        if self._token and time.time() < self._token_expiry - 60:
            return
        with self._token_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            try:
                resp = self._session.post(
                    TOKEN_URL,
                    auth=(self._key, self._secret),
                    data={"grant_type": "client_credentials"},
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise VasttrafikConnectionError(f"Token request failed: {exc}") from exc
            if resp.status_code in (400, 401, 403):
                raise ConfigEntryAuthFailed("Västtrafik rejected the API key or secret")
            try:
                resp.raise_for_status()
                token = resp.json().get("access_token")
                expires_in = resp.json().get("expires_in", 3600)
            except (requests.RequestException, ValueError, AttributeError) as exc:
                raise VasttrafikConnectionError(f"Token request failed: {exc}") from exc
            if not token:
                raise VasttrafikConnectionError("Token response had no access_token")
            self._token = token
            self._token_expiry = time.time() + float(expires_in or 3600)
            self._session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, url: str, params: dict | None = None) -> requests.Response:
        self.ensure_token()
        resp = self._session.get(url, params=params, timeout=15)
        if resp.status_code == 401:  # token revoked early — refresh once and retry
            self._token = None
            self._token_expiry = 0.0
            self.ensure_token()
            resp = self._session.get(url, params=params, timeout=15)
        return resp

    def _get(self, path: str, params: dict | None = None, base: str = BASE_URL) -> Any:
        resp = self._request(f"{base}{path}", params)
        resp.raise_for_status()
        return resp.json()

    def _optional(
        self, api: str, url: str, params: dict | None = None
    ) -> requests.Response | None:
        """GET from an API the application may not be subscribed to. Returns
        None when it is refused (then left alone for a while), has no content,
        or the resource does not exist; other errors raise."""
        if api in self.disabled_apis or time.monotonic() < self._optional_blocked.get(api, 0.0):
            return None
        resp = self._request(url, params)
        if resp.status_code in (401, 403):
            self._optional_blocked[api] = time.monotonic() + OPTIONAL_API_RETRY_SECONDS
            _LOGGER.info(
                "The Västtrafik %s API is not enabled for this application (HTTP %s); "
                "its extra data is skipped", api, resp.status_code,
            )
            return None
        if resp.status_code in (204, 404):
            return None
        resp.raise_for_status()
        return resp

    def optional_api_available(self, api: str) -> bool:
        if api in self.disabled_apis:
            return False
        if api == "Störning":
            return not self._störning_unavailable
        return time.monotonic() >= self._optional_blocked.get(api, 0.0)

    def probe_optional_apis(self) -> dict[str, bool | None]:
        """Whether this application may use each API beyond Planera Resa:
        True, False (the gateway answers 401/403), or None when it could not be
        asked. Each probe is one small request; any other answer, even an
        error about the request itself, shows the door is open."""
        probes = {
            "Störning": (f"{STÖRNING_URL}/traffic-situations/stoparea/0000000000000000", None),
            "Geografi": (f"{GEO_URL}/TariffZones", {"limit": 1}),
            "Pendelparkering": (f"{SPP_URL}/parkings", {"max": 1}),
        }
        access: dict[str, bool | None] = {}
        for api, (url, params) in probes.items():
            try:
                access[api] = self._request(url, params).status_code not in (401, 403)
            except ConfigEntryAuthFailed:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not check access to the %s API: %s", api, exc)
                access[api] = None
        return access

    @staticmethod
    def _list(data: Any, *keys: str) -> list[Any]:
        """Return the first matching key as a list (handles dict-wrapped or bare-list)."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                val = data.get(key)
                if val is not None:
                    return [val] if isinstance(val, dict) else list(val)
        return []

    # ── Locations ─────────────────────────────────────────────────────────────

    def lookup_station(self, name: str) -> list[dict]:
        data = self._get("/locations/by-text", {"q": name, "types": "stoparea", "limit": 10})
        raw = self._list(data, "results", "stopAreas", "locations")
        out: list[dict] = []
        for item in raw:
            if "stopArea" in item:
                out.append(item["stopArea"])
            elif "gid" in item:
                out.append(item)
        return out if out else raw

    def nearby_stops(
        self, lat: float, lon: float, radius: int = 1500, limit: int = 8
    ) -> list[dict]:
        """Stop areas around a coordinate, nearest first, each with
        straightLineDistanceInMeters."""
        data = self._get("/locations/by-coordinates", {
            "latitude": lat, "longitude": lon,
            "radiusInMeters": radius, "types": "stoparea", "limit": limit,
        })
        stops = [s for s in self._list(data, "results") if s.get("gid") and s.get("name")]
        return sorted(stops, key=lambda s: s.get("straightLineDistanceInMeters") or 0)

    # ── Departures ─────────────────────────────────────────────────────────────

    def get_departures(
        self,
        stop_gid: str,
        *,
        when: Any = None,
        limit: int = 20,
        time_span_minutes: int | None = None,
        max_per_line_and_direction: int | None = None,
        max_pages: int = 1,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "includeOccupancy": "true"}
        if when is not None:
            params["startDateTime"] = when.isoformat()
        if time_span_minutes is not None:
            params["timeSpanInMinutes"] = max(0, min(1440, int(time_span_minutes)))
        if max_per_line_and_direction is not None:
            params["maxDeparturesPerLineAndDirection"] = int(max_per_line_and_direction)

        path = f"/stop-areas/{quote(stop_gid, safe='')}/departures"
        out: list[dict] = []
        for _ in range(max(1, max_pages)):
            if out:
                params["offset"] = len(out)
            data = self._get(path, params)
            page = self._list(data, "results")
            out.extend(page)
            has_next = isinstance(data, dict) and (data.get("links") or {}).get("next")
            if not page or not has_next:
                break
        return out

    def get_departure_window(
        self, stop_gid: str, *, when: Any, time_span_minutes: int
    ) -> list[dict]:
        """Every departure from a stop in a time window. Briefly cached so the
        lines monitored at one stop share a single request per refresh."""
        key = (stop_gid, int(time_span_minutes))
        with self._dep_lock:
            hit = self._dep_cache.get(key)
            if hit and time.monotonic() - hit[0] < DEPARTURE_CACHE_SECONDS:
                return hit[1]
            data = self._fetch_window(stop_gid, when, time_span_minutes)
            self._dep_cache[key] = (time.monotonic(), data)
            return data

    def _fetch_window(self, stop_gid: str, when: Any, time_span_minutes: int) -> list[dict]:
        caps = self._per_line_caps
        for i, cap in enumerate(caps):
            try:
                data = self.get_departures(
                    stop_gid,
                    when=when,
                    limit=DEPARTURE_PAGE_SIZE,
                    time_span_minutes=time_span_minutes,
                    max_per_line_and_direction=cap,
                    max_pages=DEPARTURE_MAX_PAGES,
                )
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code == 400 and i + 1 < len(caps):
                    _LOGGER.debug("maxDeparturesPerLineAndDirection=%s refused, trying lower", cap)
                    continue
                raise
            self._per_line_caps = caps[i:]  # remember what the server accepts
            return data
        return []

    def get_departure_details(
        self,
        stop_gid: str,
        details_reference: str,
        includes: list[str] | None = None,
    ) -> dict:
        params: dict[str, Any] = {}
        if includes:
            params["includes"] = includes  # requests repeats multi-value params
        return self._get(
            f"/stop-areas/{quote(stop_gid, safe='')}"
            f"/departures/{quote(details_reference, safe='')}/details",
            params,
        )

    def resolve_terminus_gid(self, stop_gid: str, details_reference: str) -> str:
        """Terminus (last call) stop-area gid for a departure — used as directionGid.
        Not on the plain departure object; read from the details call. "" on failure."""
        try:
            data = self.get_departure_details(
                stop_gid, details_reference, includes=["servicejourneycalls"]
            )
            sjs = data.get("serviceJourneys") or []
            if not sjs:
                return ""
            calls = (sjs[0] or {}).get("callsOnServiceJourney") or []
            if not calls:
                return ""
            stop_area = ((calls[-1] or {}).get("stopPoint") or {}).get("stopArea") or {}
            return stop_area.get("gid") or ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Terminus GID resolution failed for %s: %s", details_reference, exc)
            return ""

    def get_arrivals(
        self,
        stop_gid: str,
        *,
        when: Any = None,
        limit: int = 10,
        time_span_minutes: int | None = None,
        max_per_line_and_direction: int | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if when is not None:
            params["startDateTime"] = when.isoformat()
        if time_span_minutes is not None:
            params["timeSpanInMinutes"] = max(0, min(1440, int(time_span_minutes)))
        if max_per_line_and_direction is not None:
            params["maxArrivalsPerLineAndDirection"] = int(max_per_line_and_direction)
        data = self._get(f"/stop-areas/{quote(stop_gid, safe='')}/arrivals", params)
        return self._list(data, "results")

    # ── Positions ──────────────────────────────────────────────────────────────

    def get_vehicle_positions(
        self,
        lower_left: tuple[float, float],
        upper_right: tuple[float, float],
        line_designations: list[str] | None = None,
        details_references: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Vehicle positions in a bounding box. Positions are dead-reckoned by the
        API, not live GPS. Returns [] on 404/501 (endpoint not in subscription)."""
        params: dict[str, Any] = {
            "lowerLeftLat":   lower_left[0],
            "lowerLeftLong":  lower_left[1],
            "upperRightLat":  upper_right[0],
            "upperRightLong": upper_right[1],
            "limit":          limit,
        }
        if line_designations:
            params["lineDesignations"] = line_designations
        if details_references:
            params["detailsReferences"] = details_references

        try:
            data = self._get("/positions", params)
            if isinstance(data, list):
                return data
            return self._list(data, "results", "positions", "vehiclePositions")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (404, 501):
                return []
            raise

    # ── Journey planner ────────────────────────────────────────────────────────

    def plan_journey(
        self,
        origin_gid: str,
        destination_gid: str,
        *,
        when: Any = None,
        limit: int = 5,
        only_direct: bool = False,
        transport_modes: list[str] | None = None,
        include_occupancy: bool = False,
        origin_coords: tuple[float, float] | None = None,
        destination_coords: tuple[float, float] | None = None,
        origin_park: bool = False,
        via_gid: str | None = None,
        arrive_by: bool = False,
    ) -> dict:
        """Search journeys. Either end can be a coordinate instead of a stop; the
        result then starts or ends with an access link (the walk, or the drive
        to a park-and-ride when *origin_park* is set)."""
        params: dict[str, Any] = {
            "dateTimeRelatesTo": "arrival" if arrive_by else "departure",
            "limit":           limit,
        }
        if origin_coords:
            params.update(originLatitude=origin_coords[0], originLongitude=origin_coords[1],
                          originName="Home")
            if origin_park:
                params["originPark"] = "1,,"
        else:
            params["originGid"] = origin_gid
        if destination_coords:
            params.update(destinationLatitude=destination_coords[0],
                          destinationLongitude=destination_coords[1], destinationName="Home")
        else:
            params["destinationGid"] = destination_gid
        if via_gid:
            params["viaGid"] = via_gid
        if when is not None:
            params["dateTime"] = when.isoformat()
        if only_direct:
            params["onlyDirectConnections"] = "true"
        if transport_modes:
            params["transportModes"] = transport_modes
        if include_occupancy:
            params["includeOccupancy"] = "true"
        return self._get("/journeys", params)

    def journey_details(self, details_reference: str, includes: list[str]) -> dict:
        return self._get(
            f"/journeys/{quote(details_reference, safe='')}/details", {"includes": includes}
        )

    def line_directions(self, origin_gid: str, destination_gid: str) -> dict[str, str]:
        """Lines that run origin→destination, each with the headsign to board.
        Direct connections first; journeys with a change only if there are none."""
        for only_direct in (True, False):
            plan = self.plan_journey(
                origin_gid, destination_gid, limit=10, only_direct=only_direct
            )
            found = journey_line_directions(plan, origin_gid)
            if found:
                return found
        return {}

    # ── Ticket pricing ─────────────────────────────────────────────────────────

    def get_journey_ticket(self, origin_gid: str, destination_gid: str) -> list[dict]:
        """Cheapest ticket products between two stop areas
        (TicketSpecificationApiModel[]). [] on any error."""
        try:
            data = self._get("/products/journeyticket", {
                "originGid":      origin_gid,
                "destinationGid": destination_gid,
            })
            if isinstance(data, list):
                return data
            return self._list(data, "results", "tickets")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Ticket fetch failed for %s→%s: %s", origin_gid, destination_gid, exc)
            return []

    # ── Geografi v3 (optional) ─────────────────────────────────────────────────

    def stop_area_info(self, gid: str) -> dict | None:
        """Static facts about a stop area: tariff zones and its stop points."""
        resp = self._optional("Geografi", f"{GEO_URL}/StopAreas/{quote(gid, safe='')}", {
            "includeStopPoints": "true", "includeTariffZones": "true", "srid": 4326,
        })
        data = resp.json() if resp is not None else None
        return (data or {}).get("stopArea") if isinstance(data, dict) else None

    def commuter_parking_area(self, number: int) -> dict | None:
        resp = self._optional(
            "Geografi", f"{GEO_URL}/CommuterParkingAreas/{int(number)}",
            {"includeCommuterParkingLots": "true"},
        )
        data = resp.json() if resp is not None else None
        return (data or {}).get("commuterParkingArea") if isinstance(data, dict) else None

    # ── Pendelparkering / SPP v3 (optional) ────────────────────────────────────

    def parkings_for_stop(self, stop_gid: str) -> list[dict] | None:
        """Commuter parking areas tied to a stop area, with live free spaces.
        None when the API is not available to this application."""
        resp = self._optional("Pendelparkering", f"{SPP_URL}/parkings", {"stopArea": stop_gid})
        if resp is None:
            return None if not self.optional_api_available("Pendelparkering") else []
        data = resp.json()
        return data if isinstance(data, list) else []

    def parking_full_forecast(self, parking_id: int, date: str) -> str | None:
        """When the parking is expected to fill up on *date*; None if it is not."""
        resp = self._optional(
            "Pendelparkering", f"{SPP_URL}/forecastFullTime/{int(parking_id)}/{date}"
        )
        if resp is None:
            return None
        try:
            value = resp.json()
        except ValueError:
            value = resp.text
        return str(value).strip().strip('"') or None

    def parking_forecast_free(self, parking_id: int, when: Any) -> int | None:
        """Forecast number of free spaces at a point in time (up to a week ahead)."""
        resp = self._optional(
            "Pendelparkering",
            f"{SPP_URL}/forecastAvailability/{int(parking_id)}/{when.strftime('%Y%m%d%H%M')}",
        )
        if resp is None:
            return None
        try:
            return int(resp.json())
        except (ValueError, TypeError):
            return None

    def parking_image(self, parking_id: int, camera_id: int) -> tuple[bytes, str] | None:
        resp = self._optional(
            "Pendelparkering", f"{SPP_URL}/parkingImages/{int(parking_id)}/{int(camera_id)}"
        )
        if resp is None or not resp.content:
            return None
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")

    # ── Störning v1 ────────────────────────────────────────────────────────────

    def _störning_get(self, path: str) -> list[dict]:
        """GET a Störning path. Self-healing: retries once on 401 and clears the
        unavailable flag on any success. An application without the API gets
        403 for every call, so after one it is only asked again once an hour."""
        if "Störning" in self.disabled_apis:
            return []
        if self._störning_unavailable and time.monotonic() < self._störning_retry_at:
            return []
        self.ensure_token()
        try:
            resp = self._session.get(f"{STÖRNING_URL}{path}", timeout=15)
            if resp.status_code == 401:
                self._token = None
                self._token_expiry = 0.0
                self.ensure_token()
                resp = self._session.get(f"{STÖRNING_URL}{path}", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._störning_unavailable = False
            self._störning_warned = False
            return data if isinstance(data, list) else []
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (403, 404):
                self._störning_unavailable = True
                if code == 403:
                    self._störning_retry_at = time.monotonic() + STÖRNING_RETRY_SECONDS
                if not self._störning_warned:
                    _LOGGER.info(
                        "The Västtrafik Störning (TrafficSituations) API answered HTTP %d: "
                        "it is not enabled for this application, so the disruption "
                        "sensors are unavailable. Add the API to the application at "
                        "developer.vasttrafik.se and reload the integration", code,
                    )
                    self._störning_warned = True
                return []
            raise

    def get_traffic_situations_for_line(self, line_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/line/{quote(line_gid, safe='')}")

    def get_traffic_situations_for_stoparea(self, stop_area_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/stoparea/{quote(stop_area_gid, safe='')}")
