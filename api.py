"""Västtrafik Planera Resa v4 API adapter — corrected against the published OAS 3.0 spec."""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import requests

from homeassistant.exceptions import ConfigEntryAuthFailed

_LOGGER = logging.getLogger(__name__)

BASE_URL    = "https://ext-api.vasttrafik.se/pr/v4"
STÖRNING_URL = "https://ext-api.vasttrafik.se/ts/v1"
TOKEN_URL   = "https://ext-api.vasttrafik.se/token"


class VtjpAdapter:
    """Full adapter for the Västtrafik Planera Resa v4 REST API (OAS 3.0)."""

    def __init__(self, key: str, secret: str, language: str = "sv") -> None:
        self._key    = key
        self._secret = secret
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            # REST.md section 12: "sv" or "en". Affects notes, disruption text,
            # maneuverDescription across Journeys, Stop-Areas and Positions endpoints.
            "Accept-Language": language if language in ("sv", "en") else "sv",
        })
        self._token: str | None = None
        self._token_expiry: float = 0.0
        # Set True on first 403/404 so we stop logging every poll
        self._störning_unavailable: bool = False

    # ── Auth ──────────────────────────────────────────────────────────────────

    def ensure_token(self) -> None:
        """Obtain or refresh the OAuth 2.0 client-credentials token."""
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
            _LOGGER.debug("Token refreshed, valid %ds", payload.get("expires_in", 3600))
        except Exception as exc:
            _LOGGER.error("Token refresh failed: %s", exc)
            raise ConfigEntryAuthFailed("Västtrafik token request failed") from exc

    # ── Low-level ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None, base: str = BASE_URL) -> Any:
        self.ensure_token()
        resp = self._session.get(f"{base}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _list(data: Any, *keys: str) -> list[Any]:
        """
        Pull the first matching key from *data* and return it as a list.
        Handles both dict-wrapped and bare-list responses.
        """
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
        """Search stop areas by text.  Returns list of location dicts."""
        data = self._get("/locations/by-text", {"q": name, "types": "stoparea", "limit": 10})
        # v4 wraps results under "results" which is a list of location objects
        raw = self._list(data, "results", "stopAreas", "locations")
        # Each item may be wrapped under a "stopArea" key
        out: list[dict] = []
        for item in raw:
            if "stopArea" in item:
                out.append(item["stopArea"])
            elif "gid" in item:
                out.append(item)
        return out if out else raw

    def lookup_by_coordinates(self, lat: float, lon: float, radius: int = 500) -> list[dict]:
        """Find stop areas near (lat, lon)."""
        data = self._get("/locations/by-coordinates", {
            "latitude": lat, "longitude": lon,
            "radiusInMeters": radius, "types": "stoparea", "limit": 10,
        })
        return self._list(data, "results", "stopAreas", "locations")

    # ── Stop areas ─────────────────────────────────────────────────────────────

    def get_stop_area(self, gid: str) -> dict:
        """
        Fetch stop-area metadata including coordinates.

        OAS: GET /stop-areas returns [{gid, name, lat, long}]
        Note: the coordinate fields are 'lat' and 'long' (NOT 'latitude'/'longitude')
        on the StopAreaApiModel. The returned dict normalises both to ensure
        callers can use either form.
        """
        data = self._get("/stop-areas")
        if isinstance(data, list):
            match = next((s for s in data if s.get("gid") == gid), {})
            if match:
                # Normalise: add 'latitude'/'longitude' aliases for callers
                # that expect the same shape as stopPoint (which does use latitude/longitude)
                if "lat" in match and "latitude" not in match:
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
    ) -> list[dict]:
        """
        Upcoming departures from a stop area.

        OAS response: { "results": [ DepartureApiModel, ... ] }

        Time fields (section 6.2.1 of REST.md):
          plannedTime                    — always present
          estimatedTime                  — present when realtime data exists
          estimatedOtherwisePlannedTime  — estimated if available, otherwise planned.
                                           Always use this for display; it is the API's
                                           own recommended "best time" field.

        Other key fields:
          detailsReference               — use to fetch details / coordinates / occupancy
          serviceJourney.line.shortName / transportMode / backgroundColor / isWheelchairAccessible
          serviceJourney.direction       — direction label string
          serviceJourney.isRealtimeJourney — true for emergency extra trips (no vehicle type info)
          stopPoint.{platform, latitude, longitude}
          realtimeStopPoint              — populated when stop is moved; use instead of stopPoint
          isCancelled / isPartCancelled
          occupancy.level                (low/medium/high) — requires includeOccupancy=true
        """
        params: dict[str, Any] = {"limit": limit, "includeOccupancy": "true"}
        if when is not None:
            params["startDateTime"] = when.isoformat()
        if direction_gid:
            params["directionGid"] = direction_gid
        data = self._get(f"/stop-areas/{quote(stop_gid, safe='')}/departures", params)
        return self._list(data, "results")

    def get_departure_details(
        self,
        stop_gid: str,
        details_reference: str,
        includes: list[str] | None = None,
    ) -> dict:
        """
        Departure details for one specific departure.

        OAS: GET /stop-areas/{stopAreaGid}/departures/{detailsReference}/details
             ?includes=servicejourneycalls,servicejourneycoordinates,occupancy

        Key fields in response:
          serviceJourneys[0].serviceJourneyCoordinates
              → [{latitude, longitude, elevation}]  — actual GPS path of the route
          serviceJourneys[0].callsOnServiceJourney
              → [{stopPoint.{name, latitude, longitude},
                  plannedArrivalTime, estimatedArrivalTime,
                  plannedDepartureTime, estimatedDepartureTime,
                  isCancelled}]
          occupancy.level
        """
        params: dict[str, Any] = {}
        if includes:
            params["includes"] = includes  # requests repeats multi-value correctly
        return self._get(
            f"/stop-areas/{quote(stop_gid, safe='')}"
            f"/departures/{quote(details_reference, safe='')}/details",
            params,
        )

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
        """
        Vehicle positions within a bounding box.

        KEY INSIGHT — REST.md section 9.1:
          "Positionerna är ungefärliga och bygger på tiden som gått från senaste
           hållplats, som fordonet stannat vid, och sträckan till nästa planerade
           hållplats."
          = Positions are approximate dead-reckoning from the last visited stop,
            NOT live GPS. The API uses average speed along the planned route.
            This is exactly what our Tier 2 path interpolation does, so the two
            methods produce equivalent accuracy.

        Filtering (REST.md 9.1.1 / 9.1.2):
          detailsReferences — pin to specific vehicle(s) by detailsReference value
          lineDesignations  — filter by line.name (case-sensitive, e.g. "16")

        Response: direct JSON array, latitude/longitude are top-level on each item.
        Returns [] on 404/501 (endpoint not in subscription).
        """
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
                _LOGGER.debug(
                    "/positions returned %d — endpoint not in your API subscription", code
                )
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
        """Plan trips between two stop areas."""
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

    def get_journey_ticket(
        self,
        origin_gid: str,
        destination_gid: str,
    ) -> list[dict]:
        """
        Cheapest ticket products for a journey between two stop areas.

        OAS: GET /products/journeyticket?originGid=...&destinationGid=...

        Response: list of {
          ticketName: str,
          productType: "single" | ...,
          configurations: [{
            productId, validityLength, itemPrice,
            ageType: "adult" | "youth" | "senior" | ...,
            zoneIds: [str],
          }]
        }

        Returns [] on any error (ticket pricing is informational, not critical).
        """
        try:
            data = self._get("/products/journeyticket", {
                "originGid":      origin_gid,
                "destinationGid": destination_gid,
            })
            if isinstance(data, list):
                return data
            return self._list(data, "results", "tickets")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            _LOGGER.debug("Ticket endpoint returned %d for %s→%s", code, origin_gid, destination_gid)
            return []
        except Exception as exc:
            _LOGGER.debug("Ticket fetch failed: %s", exc)
            return []

    # ── Störning v1 ────────────────────────────────────────────────────────────

    def _störning_get(self, path: str) -> list[dict]:
        """
        GET a Störning v1 path, returning a list.
        Handles auth, 403/404 subscription errors, and bare-list responses.

        OAS 2.0 base URL: ext-api.vasttrafik.se/ts/v1
        All endpoints return a direct JSON array — never wrapped in a dict key.
        """
        if self._störning_unavailable:
            return []
        self.ensure_token()
        try:
            resp = self._session.get(
                f"{STÖRNING_URL}{path}", timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (403, 404):
                self._störning_unavailable = True
                _LOGGER.warning(
                    "Störning API returned %d — a separate Störning subscription is "
                    "required on developer.vasttrafik.se. Disruption sensors will be "
                    "unavailable. (This message will not repeat.)",
                    code,
                )
                return []
            raise

    def get_traffic_situations_for_line(self, line_gid: str) -> list[dict]:
        """
        Traffic situations for a specific line.

        OAS: GET /traffic-situations/line/{gid}
        path param: gid — the line GID, e.g. "9011014500100000"

        Best used when CONF_LINE_GID is available (stored at config time from
        the live departures picker).
        """
        return self._störning_get(f"/traffic-situations/line/{quote(line_gid, safe='')}")

    def get_traffic_situations_for_stoparea(self, stop_area_gid: str) -> list[dict]:
        """
        Traffic situations for a stop area.

        OAS: GET /traffic-situations/stoparea/{gid}
        path param: gid — stop area GID, e.g. "9021014003310000"
        """
        return self._störning_get(f"/traffic-situations/stoparea/{quote(stop_area_gid, safe='')}")

    def get_traffic_situations_for_stoppoint(self, stop_point_gid: str) -> list[dict]:
        """
        Traffic situations for a specific stop point (platform level).

        OAS: GET /traffic-situations/stoppoint/{gid}
        """
        return self._störning_get(f"/traffic-situations/stoppoint/{quote(stop_point_gid, safe='')}")

    def get_traffic_situations_for_journey(self, journey_gid: str) -> list[dict]:
        """
        Traffic situations for a specific service journey.

        OAS: GET /traffic-situations/journey/{gid}
        Use the service_journey_gid from departure attributes to get disruptions
        affecting the exact journey the user is tracking.
        """
        return self._störning_get(f"/traffic-situations/journey/{quote(journey_gid, safe='')}")

    def get_all_traffic_situations(self) -> list[dict]:
        """
        All current and future traffic situations (no filter).

        OAS: GET /traffic-situations  — no parameters accepted.
        """
        return self._störning_get("/traffic-situations")

    # Backwards-compat shim — existing binary_sensor.py still calls this
    def get_traffic_situations(
        self,
        line_gid: str | None = None,
        stop_gid: str | None = None,
    ) -> list[dict]:
        """
        Compatibility wrapper: routes to the correct path-based endpoint.

        Preference order (most specific → least):
          1. line_gid  → GET /traffic-situations/line/{gid}
          2. stop_gid  → GET /traffic-situations/stoparea/{gid}
          3. neither   → GET /traffic-situations  (all)
        """
        if line_gid:
            return self.get_traffic_situations_for_line(line_gid)
        if stop_gid:
            return self.get_traffic_situations_for_stoparea(stop_gid)
        return self.get_all_traffic_situations()


