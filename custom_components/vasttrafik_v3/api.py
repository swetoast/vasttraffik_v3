"""Adapter for the Västtrafik Planera Resa v4 and Störning v1 REST APIs."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from homeassistant.exceptions import ConfigEntryAuthFailed

_LOGGER = logging.getLogger(__name__)

BASE_URL     = "https://ext-api.vasttrafik.se/pr/v4"
STÖRNING_URL = "https://ext-api.vasttrafik.se/ts/v1"
TOKEN_URL    = "https://ext-api.vasttrafik.se/token"


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

    @property
    def disruptions_unavailable(self) -> bool:
        return self._störning_unavailable

    # ── Auth ──────────────────────────────────────────────────────────────────

    def ensure_token(self) -> None:
        """Obtain/refresh the client-credentials token. Thread-safe: several
        executor threads share one Session and must not race on the header."""
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
                resp.raise_for_status()
                payload = resp.json()
                token = payload.get("access_token")
                if not token:
                    raise ValueError("No access_token in response")
                self._token = token
                self._token_expiry = time.time() + payload.get("expires_in", 3600)
                self._session.headers["Authorization"] = f"Bearer {token}"
            except Exception as exc:
                _LOGGER.error("Token refresh failed: %s", exc)
                raise ConfigEntryAuthFailed("Västtrafik token request failed") from exc

    def _get(self, path: str, params: dict | None = None, base: str = BASE_URL) -> Any:
        self.ensure_token()
        resp = self._session.get(f"{base}{path}", params=params, timeout=15)
        if resp.status_code == 401:  # token revoked early — refresh once and retry
            self._token = None
            self._token_expiry = 0.0
            self.ensure_token()
            resp = self._session.get(f"{base}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

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

    def lookup_by_coordinates(self, lat: float, lon: float, radius: int = 500) -> list[dict]:
        data = self._get("/locations/by-coordinates", {
            "latitude": lat, "longitude": lon,
            "radiusInMeters": radius, "types": "stoparea", "limit": 10,
        })
        return self._list(data, "results", "stopAreas", "locations")

    # ── Stop areas ─────────────────────────────────────────────────────────────

    def get_stop_area(self, gid: str) -> dict:
        """Unused. WARNING: /stop-areas has no filter — it returns the whole
        registry; scan/cache once if ever needed. Coordinate fields are lat/long."""
        data = self._get("/stop-areas")
        if isinstance(data, list):
            match = next((s for s in data if s.get("gid") == gid), {})
            if match and "lat" in match and "latitude" not in match:
                match = dict(match)
                match["latitude"] = match["lat"]
                match["longitude"] = match.get("long")
            return match
        return {}

    # ── Departures ─────────────────────────────────────────────────────────────

    def get_departures(
        self,
        stop_gid: str,
        *,
        when: Any = None,
        direction_gid: str | None = None,
        limit: int = 20,
        time_span_minutes: int | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "includeOccupancy": "true"}
        if when is not None:
            params["startDateTime"] = when.isoformat()
        if direction_gid:
            params["directionGid"] = direction_gid
        if time_span_minutes is not None:
            params["timeSpanInMinutes"] = max(0, min(1440, int(time_span_minutes)))
        data = self._get(f"/stop-areas/{quote(stop_gid, safe='')}/departures", params)
        return self._list(data, "results")

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
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if when is not None:
            params["startDateTime"] = when.isoformat()
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
    ) -> dict:
        params: dict[str, Any] = {
            "originGid":       origin_gid,
            "destinationGid":  destination_gid,
            "dateTimeRelatesTo": "departure",
            "limit":           limit,
        }
        if when is not None:
            params["dateTime"] = when.isoformat()
        if only_direct:
            params["onlyDirectConnections"] = "true"
        if transport_modes:
            params["transportModes"] = transport_modes
        return self._get("/journeys", params)

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

    # ── Störning v1 ────────────────────────────────────────────────────────────

    def _störning_get(self, path: str) -> list[dict]:
        """GET a Störning path. Self-healing: always attempts, retries once on 401,
        and clears the unavailable flag on any success (no permanent latch)."""
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
                if not self._störning_warned:
                    _LOGGER.debug(
                        "Störning API %s returned %d — disruptions unavailable "
                        "(check the TrafficSituations API is added to your app)", path, code
                    )
                    self._störning_warned = True
                return []
            raise

    def get_traffic_situations_for_line(self, line_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/line/{quote(line_gid, safe='')}")

    def get_traffic_situations_for_stoparea(self, stop_area_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/stoparea/{quote(stop_area_gid, safe='')}")

    def get_traffic_situations_for_stoppoint(self, stop_point_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/stoppoint/{quote(stop_point_gid, safe='')}")

    def get_traffic_situations_for_journey(self, journey_gid: str) -> list[dict]:
        return self._störning_get(f"/traffic-situations/journey/{quote(journey_gid, safe='')}")

    def get_all_traffic_situations(self) -> list[dict]:
        return self._störning_get("/traffic-situations")

    def get_traffic_situations(
        self,
        line_gid: str | None = None,
        stop_gid: str | None = None,
    ) -> list[dict]:
        """Route to the most specific traffic-situations endpoint available."""
        if line_gid:
            return self.get_traffic_situations_for_line(line_gid)
        if stop_gid:
            return self.get_traffic_situations_for_stoparea(stop_gid)
        return self.get_all_traffic_situations()
