"""Shared helpers used across sensor, binary_sensor, and device_tracker."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_DIRECTION,
    CONF_DIRECTION_GID,
    CONF_END_STOP_GID,
    CONF_LINE_NAME,
    CONF_STOP_GID,
)


def dir_key_for_line(ml: dict) -> str:
    """Direction discriminator so the same line+stop in two directions doesn't
    collapse onto one device/unique_id. GID → end-stop GID → direction slug → any.
    (The text slug matters: direction GID is often absent for direction-only setups.)"""
    direction_slug = (ml.get(CONF_DIRECTION) or "").strip().lower().replace(" ", "_")
    return (
        ml.get(CONF_DIRECTION_GID)
        or ml.get(CONF_END_STOP_GID)
        or direction_slug
        or "any"
    )


def line_key(ml: dict) -> str:
    """Identity of a monitored line; unique IDs and the device are built on it."""
    return f"{ml.get(CONF_STOP_GID, '')}_{ml.get(CONF_LINE_NAME, '')}_{dir_key_for_line(ml)}"


def short_direction(value: str | None) -> str:
    """Reduce a headsign to its terminus token for tolerant direction matching:
    'Kungssten via Centrum, Påstigning fram' → 'kungssten'. The suffixes
    (', Påstigning fram', ' via …') vary per trip on the same travel direction."""
    s = (value or "").lower().strip()
    s = s.split(",")[0]
    s = s.split(" via ")[0]
    return s.strip()


def journey_line_directions(plan: dict, origin_gid: str) -> dict[str, str]:
    """Map line short name → headsign for trips that board at *origin_gid* in a
    journey plan. The plan runs origin→destination, so this headsign is the
    right travel direction even when the destination is a mid-route stop that
    no headsign mentions."""
    out: dict[str, str] = {}
    for result in plan.get("results") or []:
        for leg in result.get("tripLegs") or []:
            sj = leg.get("serviceJourney") or {}
            short = (sj.get("line") or {}).get("shortName") or ""
            direction = (sj.get("direction") or "").strip()
            stop_point = (leg.get("origin") or {}).get("stopPoint") or {}
            boarded_at = (stop_point.get("stopArea") or {}).get("gid")
            if not short or not direction or short in out:
                continue
            if boarded_at and boarded_at != origin_gid:
                continue  # a later leg of a journey with a change
            out[short] = direction
    return out


def parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a tz-aware datetime; None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def hhmm(value: datetime | None) -> str | None:
    """Clock time in Home Assistant's configured time zone."""
    return dt_util.as_local(value).strftime("%H:%M") if value else None


def to_float(value: Any) -> float | None:
    """Safely cast to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def best_departure_dt(dep: dict) -> datetime | None:
    """Best known time of a departure: estimated when available, else planned."""
    for key in ("estimatedOtherwisePlannedTime", "estimatedTime", "plannedTime"):
        dt = parse_dt(dep.get(key))
        if dt:
            return dt
    return None


def walk_minutes(distance_m: float | None) -> int:
    """Walk-time estimate from a straight-line distance: 20 % detour at 4.5 km/h."""
    if not distance_m or distance_m <= 0:
        return 0
    return max(1, round(distance_m * 1.2 / 75))


def _call_matches(call: dict, gid: str) -> bool:
    stop_point = call.get("stopPoint") or {}
    return gid in ((stop_point.get("stopArea") or {}).get("gid"), stop_point.get("gid"))


def boarding_index(calls: list[dict], stop_gid: str) -> int | None:
    return next((i for i, c in enumerate(calls) if _call_matches(c, stop_gid)), None)


def destination_call(calls: list[dict], stop_gid: str, end_gid: str) -> dict | None:
    """The call at *end_gid* that comes after boarding at *stop_gid*, if the trip
    goes that way. This is what decides a trip's direction: no headsign needed."""
    start = boarding_index(calls, stop_gid)
    first = 0 if start is None else start + 1
    return next((c for c in calls[first:] if _call_matches(c, end_gid)), None)


def _best_time(obj: dict | None, *keys: str) -> datetime | None:
    for key in keys:
        value = parse_dt((obj or {}).get(key))
        if value:
            return value
    return None


def _access_link(link: dict | None, *, leaving: bool) -> dict | None:
    """Walk or drive between a coordinate and a stop, at one end of a journey."""
    if not link:
        return None
    endpoint = link.get("origin") if leaving else link.get("destination")
    moment = _best_time(
        link,
        "estimatedDepartureTime" if leaving else "estimatedArrivalTime",
        "plannedDepartureTime" if leaving else "plannedArrivalTime",
    ) or _best_time(endpoint, "estimatedOtherwisePlannedTime", "plannedTime")
    minutes = link.get("estimatedDurationInMinutes")
    if minutes is None:
        minutes = link.get("plannedDurationInMinutes")
    return {
        "mode":       link.get("transportMode") or "walk",
        "minutes":    minutes,
        "distance_m": link.get("distanceInMeters"),
        "time":       moment,
    }


def parse_journey(journey: dict) -> dict | None:
    """Flatten a Planera Resa journey to what the trip sensor shows. None for
    journeys without a public-transport leg (walk-only suggestions)."""
    legs = sorted(journey.get("tripLegs") or [], key=lambda l: l.get("journeyLegIndex") or 0)
    if not legs:
        return None
    first, last = legs[0], legs[-1]
    origin, destination = first.get("origin") or {}, last.get("destination") or {}
    departure = _best_time(origin, "estimatedOtherwisePlannedTime", "plannedTime")
    arrival = _best_time(destination, "estimatedOtherwisePlannedTime", "plannedTime")
    if departure is None:
        return None
    planned = parse_dt(origin.get("plannedTime"))

    def line_of(leg: dict) -> str:
        line = (leg.get("serviceJourney") or {}).get("line") or {}
        return line.get("shortName") or line.get("designation") or line.get("name") or "?"

    def stop_of(call: dict) -> dict:
        return call.get("realtimeStopPoint") or call.get("stopPoint") or {}

    def leg_summary(leg: dict) -> dict:
        o, d = leg.get("origin") or {}, leg.get("destination") or {}
        connecting = leg.get("estimatedConnectingTimeInMinutes")
        if connecting is None:
            connecting = leg.get("plannedConnectingTimeInMinutes")
        return {
            "line":      line_of(leg),
            "direction": (leg.get("serviceJourney") or {}).get("direction"),
            "from":      (o.get("stopPoint") or {}).get("name"),
            "to":        (d.get("stopPoint") or {}).get("name"),
            "departure": hhmm(_best_time(o, "estimatedOtherwisePlannedTime", "plannedTime")),
            "arrival":   hhmm(_best_time(d, "estimatedOtherwisePlannedTime", "plannedTime")),
            "platform":  stop_of(o).get("platform"),
            "connecting_minutes": connecting,
        }

    # Walks between legs come with the journey, so a change shows its real distance.
    transfers = [
        {
            "minutes":    link.get("estimatedDurationInMinutes", link.get("plannedDurationInMinutes")),
            "distance_m": link.get("distanceInMeters"),
            "from":       ((link.get("origin") or {}).get("stopPoint") or {}).get("name"),
            "to":         ((link.get("destination") or {}).get("stopPoint") or {}).get("name"),
        }
        for link in sorted(
            journey.get("connectionLinks") or [], key=lambda l: l.get("journeyLegIndex") or 0
        )
    ]
    notes = [
        n.get("text") for leg in legs for n in (leg.get("notes") or []) if n.get("text")
    ]
    occupancy = (journey.get("occupancy") or first.get("occupancy") or {}).get("level")
    first_line = (first.get("serviceJourney") or {}).get("line") or {}
    board, alight = origin.get("stopPoint") or {}, destination.get("stopPoint") or {}
    return {
        "details_reference": journey.get("detailsReference"),
        "departure":         departure,
        "arrival":           arrival,
        "delay_minutes":     max(0, int((departure - planned).total_seconds() // 60)) if planned else None,
        "is_realtime":       origin.get("estimatedTime") is not None,
        "line":              line_of(first),
        "lines":             [line_of(leg) for leg in legs],
        "direction":         (first.get("serviceJourney") or {}).get("direction"),
        "transport_mode":    first_line.get("transportMode"),
        "platform":          stop_of(origin).get("platform"),
        "board_at":          board.get("name"),
        "board_gid":         (board.get("stopArea") or {}).get("gid"),
        "board_pos":         (board.get("latitude"), board.get("longitude")),
        "alight_at":         alight.get("name"),
        "alight_gid":        (alight.get("stopArea") or {}).get("gid"),
        "alight_pos":        (alight.get("latitude"), alight.get("longitude")),
        "access":            _access_link(journey.get("departureAccessLink"), leaving=True),
        "egress":            _access_link(journey.get("arrivalAccessLink"), leaving=False),
        "changes":           len(legs) - 1,
        "transfers":         transfers,
        "is_cancelled":      any(leg.get("isCancelled") for leg in legs),
        "is_part_cancelled": any(leg.get("isPartCancelled") for leg in legs),
        "risk_of_missing_connection": any(leg.get("isRiskOfMissingConnection") for leg in legs),
        "occupancy":         occupancy,
        "notes":             notes,
        "legs":              [leg_summary(leg) for leg in legs],
    }


def parse_trip_ticket(details: dict) -> dict | None:
    """Cheapest adult ticket Västtrafik suggests for a specific journey."""
    result = details.get("ticketSuggestionsResult") or {}
    suggestions = [
        t for t in (result.get("ticketSuggestions") or []) if t.get("priceInSek") is not None
    ]

    def validity_of(ticket: dict) -> str | None:
        validity = ticket.get("timeValidity") or {}
        if validity.get("amount"):
            return f"{validity['amount']} {validity.get('unit')}"
        start, end = parse_dt(validity.get("fromDateTime")), parse_dt(validity.get("toDateTime"))
        if start and end and end > start:
            return f"{int((end - start).total_seconds() // 60)} minutes"
        return None

    def cheapest(category: str) -> dict | None:
        # The API lists every product (around 200), several at the same price;
        # among equals, the one that states how long it is valid reads best.
        mine = [t for t in suggestions if (t.get("travellerCategory") or "").lower() == category]
        return min(
            mine, key=lambda t: (t["priceInSek"], not (t.get("timeValidity") or {}).get("amount"))
        ) if mine else None

    adult = cheapest("adult") or (min(suggestions, key=lambda t: t["priceInSek"]) if suggestions else None)
    if adult is None:
        return None
    youth = cheapest("youth")
    zones = [z.get("shortName") or z.get("name") for z in (details.get("tariffZones") or [])]
    return {
        "ticket_name":        adult.get("productName"),
        "ticket_price":       float(adult["priceInSek"]),
        "ticket_price_youth": float(youth["priceInSek"]) if youth else None,
        "ticket_validity":    validity_of(adult),
        "ticket_zones":       [z for z in zones if z],
    }


def stop_point_in_area(stop_point_gid: str | None, stop_area_gid: str | None) -> bool:
    """Västtrafik gids embed the stop area number: 9022 0 14 00176 0004 is a
    stop point of area 9021 0 14 00176 0000."""
    if not stop_point_gid or not stop_area_gid or len(stop_point_gid) != 16 or len(stop_area_gid) != 16:
        return False
    return stop_point_gid.startswith("9022") and stop_point_gid[4:12] == stop_area_gid[4:12]


def situation_scope(
    situation: dict, stop_gids: set[str], journey_gids: set[str]
) -> str:
    """How close a (normalised) traffic situation is to the rider:
    "trip" — names a bus they are about to take, "stop" — hits a stop they use,
    "line" — somewhere else on the line."""
    if any(j.get("gid") in journey_gids for j in situation.get("affected_journeys") or []):
        return "trip"
    points = {
        gid
        for line in situation.get("affected_lines") or []
        for gid in line.get("affected_stop_point_gids") or []
    }
    for stop in situation.get("affected_stops") or []:
        if stop.get("stop_area_gid") in stop_gids:
            return "stop"
        points.add(stop.get("gid"))
    if any(stop_point_in_area(point, area) for point in points for area in stop_gids):
        return "stop"
    return "line"


def parse_stop_info(stop_area: dict | None) -> dict:
    """Reduce a Geografi stop area to the facts the entities use."""
    if not stop_area:
        return {}
    zones = [z.get("name") or z.get("code") for z in (stop_area.get("tariffZones") or [])]
    platforms = {
        sp.get("gid"): sp.get("designation")
        for sp in (stop_area.get("stopPoints") or [])
        if sp.get("gid") and sp.get("designation")
    }
    return {
        "tariff_zones": [z for z in zones if z],
        "platforms": platforms,
        "has_local_service": stop_area.get("hasLocalService"),
    }


def parse_parking_area(area: dict, stop_gid: str) -> dict | None:
    """Normalise a Pendelparkering area. Free spaces are only reported by lots
    of type SMARTCARPARK; `free` is None when the area has none."""
    if area.get("Id") is None:
        return None
    lots = []
    for lot in area.get("ParkingLots") or []:
        smart = ((lot.get("ParkingType") or {}).get("Name") or "").upper() == "SMARTCARPARK"
        lots.append({
            "id":        lot.get("Id"),
            "name":      lot.get("Name"),
            "capacity":  lot.get("TotalCapacity"),
            "free":      lot.get("FreeSpaces") if smart else None,
            "smart":     smart,
            "barrier":   lot.get("IsRestrictedByBarrier"),
            "latitude":  lot.get("Lat"),
            "longitude": lot.get("Lon"),
            "cameras":   [c.get("Id") for c in (lot.get("ParkingCameras") or []) if c.get("Id") is not None],
        })
    counted = [l["free"] for l in lots if l["free"] is not None]
    capacities = [l["capacity"] for l in lots if l["capacity"] is not None]
    return {
        "id":       area["Id"],
        "name":     area.get("Name") or f"Parking {area['Id']}",
        "stop_gid": stop_gid,
        "lots":     lots,
        "free":     sum(counted) if counted else None,
        "capacity": sum(capacities) if capacities else None,
    }


def parse_full_forecast(value: str | None, day: datetime) -> datetime | None:
    """The forecast comes as text; accept a full timestamp or a clock time on *day*."""
    if not value:
        return None
    parsed = parse_dt(value)
    if parsed and ("T" in value or " " in value):
        return parsed
    try:
        hour, minute = (int(p) for p in value.split(":")[:2])
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None
