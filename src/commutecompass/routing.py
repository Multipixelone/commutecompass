"""Google Directions routing."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal, NamedTuple, Optional

import httpx

from commutecompass.models import Origin, ResolvedLocation, Route, TransitLeg
from commutecompass.timeutil import NYC_TZ


def _unix(dt: datetime) -> int:
    """Convert datetime to Unix timestamp."""
    return int(dt.timestamp())


def route_cache_key(origin: Origin) -> str:
    """Stable cache key for an origin.

    Rounds coordinates to ~11 m (4 decimals) so jitter in GPS-derived origins
    doesn't fragment the cache while still distinguishing real start points.
    """
    return f"{origin.lat:.4f},{origin.lon:.4f}"


class RouteTiming(NamedTuple):
    """Door-to-door timing, with unknown/provider placeholder values excluded."""

    duration_seconds: Optional[float] = None
    depart_at: Optional[datetime] = None
    arrive_at: Optional[datetime] = None


def _provider_time(value: object) -> Optional[datetime]:
    if not isinstance(value, dict):
        return None
    timestamp = value.get("value")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=NYC_TZ)
    except (ValueError, OverflowError, OSError):
        return None


def _provider_timing(candidate: object) -> RouteTiming:
    if not isinstance(candidate, dict):
        return RouteTiming()
    legs = candidate.get("legs")
    if not isinstance(legs, list) or not legs or not all(isinstance(leg, dict) for leg in legs):
        return RouteTiming()
    depart = _provider_time(legs[0].get("departure_time"))
    arrive = _provider_time(legs[-1].get("arrival_time"))
    if depart is not None and arrive is not None:
        elapsed = arrive.timestamp() - depart.timestamp()
        if elapsed >= 0:
            # Includes waits between legs as well as movement; never sum steps.
            return RouteTiming(elapsed, depart, arrive)
        depart = arrive = None

    durations: list[int] = []
    for leg in legs:
        duration = leg.get("duration")
        seconds = duration.get("value") if isinstance(duration, dict) else None
        if type(seconds) is not int or seconds < 0:
            return RouteTiming(None, depart, arrive)
        durations.append(seconds)
    return RouteTiming(float(sum(durations)), depart, arrive)


def route_timing(route: Optional[Route]) -> RouteTiming:
    """Read timing from one selected alternative, including legacy saved routes.

    Cached schedules describe a historical journey: only their duration remains
    useful for a new plan. Explicit estimates have no provider payload and keep
    their estimate timestamps. No routing requests or state writes occur here.
    """
    if route is None:
        return RouteTiming()
    if route.raw_provider_payload is None:
        depart = route.depart_at if route.depart_at.utcoffset() is not None else None
        arrive = route.arrive_at if route.arrive_at.utcoffset() is not None else None
        duration = float(route.total_duration_seconds) if route.total_duration_seconds >= 0 else None
        if depart is not None and arrive is not None:
            elapsed = arrive.timestamp() - depart.timestamp()
            if elapsed >= 0:
                duration = elapsed
            else:
                depart = arrive = None
        return RouteTiming(duration) if route.from_cache else RouteTiming(duration, depart, arrive)

    candidates = route.raw_provider_payload.get("routes")
    if not isinstance(candidates, list):
        return RouteTiming()
    if route.provider_route_index is not None:
        if route.provider_route_index >= len(candidates):
            return RouteTiming()
        timing = _provider_timing(candidates[route.provider_route_index])
    else:
        # Older parsers persisted processing-time placeholders for missing
        # timestamps. Compare only timestamps actually supplied by the provider.
        matches: list[RouteTiming] = []
        for candidate in candidates:
            candidate_timing = _provider_timing(candidate)
            depart, arrive = candidate_timing.depart_at, candidate_timing.arrive_at
            if depart is not None and (
                route.depart_at.utcoffset() is None
                or depart.timestamp() != route.depart_at.timestamp()
            ):
                continue
            if arrive is not None and (
                route.arrive_at.utcoffset() is None
                or arrive.timestamp() != route.arrive_at.timestamp()
            ):
                continue
            if depart is None and arrive is None:
                if candidate_timing.duration_seconds != route.total_duration_seconds:
                    continue
            elif depart is None or arrive is None:
                if (
                    candidate_timing.duration_seconds is not None
                    and candidate_timing.duration_seconds != route.total_duration_seconds
                ):
                    continue
            matches.append(candidate_timing)
        if not matches:
            return RouteTiming()
        timing = matches[0]
        if any(match != timing for match in matches[1:]):
            # Do not borrow an arrival/departure from another alternative.
            duration = timing.duration_seconds
            if any(match.duration_seconds != duration for match in matches[1:]):
                duration = None
            timing = RouteTiming(duration)
    if route.approximate or route.from_cache:
        return RouteTiming(timing.duration_seconds)
    return timing


# Effective door-to-door speeds (km/h) for the coarse fallback estimate.  These
# bake in waiting/transfers/parking, so they are deliberately well below vehicle
# cruising speed — the goal is a leave-time that is roughly right, not a schedule.
_FALLBACK_SPEED_KMH: dict[str, float] = {
    "transit": 18.0,
    "driving": 25.0,
    "bicycling": 14.0,
    "walking": 4.8,
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometers."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def estimate_route(
    origin: Origin,
    destination: ResolvedLocation,
    arrival_time: datetime,
    mode: Literal["transit", "driving", "walking", "bicycling"] = "transit",
) -> Optional[Route]:
    """Build a coarse distance/speed route estimate when live routing is down.

    Returns None when the destination has no coordinates (e.g. an unresolved
    station name) — there is nothing to measure against, so the caller should
    fall back to ``no_route`` rather than fabricate a number.  The returned
    route is flagged ``approximate``.
    """
    if destination.lat is None or destination.lon is None:
        return None

    distance_km = _haversine_km(origin.lat, origin.lon, destination.lat, destination.lon)
    # Crow-flies underestimates real path length; pad by 30% as a rough detour
    # factor before dividing by the mode speed.
    speed_kmh = _FALLBACK_SPEED_KMH.get(mode, _FALLBACK_SPEED_KMH["transit"])
    hours = (distance_km * 1.3) / speed_kmh
    duration_seconds = max(60, int(hours * 3600))

    depart_at = datetime.fromtimestamp(
        arrival_time.timestamp() - duration_seconds, tz=arrival_time.tzinfo,
    )
    leg = TransitLeg(
        mode=mode.upper(),  # type: ignore[arg-type]
        system=None,
        line=None,
        headsign=None,
        depart_at=depart_at,
        arrive_at=arrival_time,
        duration_seconds=duration_seconds,
        summary=f"Estimated {mode} (~{distance_km:.1f} km, live routing unavailable)",
    )
    return Route(
        legs=[leg],
        depart_at=depart_at,
        arrive_at=arrival_time,
        total_duration_seconds=duration_seconds,
        transfers=0,
        approximate=True,
    )


def _parse_step(step: dict[str, Any], nyc_tz: Any) -> Optional[TransitLeg]:
    """Parse a single step from a Directions leg into a TransitLeg.

    Returns None for unsupported travel modes.
    """
    travel_mode = step.get("travel_mode", "").upper()

    if travel_mode == "WALKING":
        mode: Literal["WALKING", "TRANSIT", "DRIVING", "BICYCLING"] = "WALKING"
    elif travel_mode == "TRANSIT":
        mode = "TRANSIT"
    elif travel_mode == "DRIVING":
        mode = "DRIVING"
    elif travel_mode == "BICYCLING":
        mode = "BICYCLING"
    else:
        return None

    duration_sec = step.get("duration", {}).get("value", 0)
    departure_time = step.get("departure_time", {})
    arrival_time = step.get("arrival_time", {})

    # Parse departure time - can be a datetime dict with "value" (unix timestamp)
    if isinstance(departure_time, dict):
        dep_ts = departure_time.get("value")
        depart_at = datetime.fromtimestamp(dep_ts, tz=nyc_tz) if dep_ts else datetime.now(nyc_tz)
    else:
        depart_at = datetime.now(nyc_tz)

    if isinstance(arrival_time, dict):
        arr_ts = arrival_time.get("value")
        arrive_at = datetime.fromtimestamp(arr_ts, tz=nyc_tz) if arr_ts else datetime.now(nyc_tz)
    else:
        arrive_at = datetime.now(nyc_tz)

    system: Optional[str] = None
    line: Optional[str] = None
    headsign: Optional[str] = None
    departure_stop_name: Optional[str] = None
    arrival_stop_name: Optional[str] = None
    summary = ""

    if mode == "TRANSIT":
        transit_details = step.get("transit_details", {})
        line_info = transit_details.get("line", {})

        # Detect system from vehicle type or agencies
        vehicle = line_info.get("vehicle", {})
        vehicle_type = vehicle.get("type", "").upper()
        agencies = line_info.get("agencies", [])

        if vehicle_type == "SUBWAY":
            system = "MTA Subway"
        elif vehicle_type == "RAIL":
            # Could be LIRR, Amtrak, etc.
            for agency in agencies:
                agency_name = agency.get("name", "")
                if "LIRR" in agency_name or "Long Island" in agency_name:
                    system = "LIRR"
                    break
                elif "MTA" in agency_name:
                    system = "MTA Subway"
                    break
            if system is None:
                system = "Rail"
        elif vehicle_type == "BUS":
            system = "MTA Bus"
        else:
            # Check agencies for hints
            for agency in agencies:
                agency_name = agency.get("name", "")
                if "MTA" in agency_name:
                    system = agency_name
                    break

        line = line_info.get("short_name") or line_info.get("name") or None

        departure_stop = transit_details.get("departure_stop", {})
        arrival_stop = transit_details.get("arrival_stop", {})
        headsign = transit_details.get("headsign")

        dep_name = departure_stop.get("name", "Unknown")
        arr_name = arrival_stop.get("name", "Unknown")
        departure_stop_name = dep_name if dep_name != "Unknown" else None
        arrival_stop_name = arr_name if arr_name != "Unknown" else None
        summary = f"{line or 'Transit'} from {dep_name} to {arr_name}"
    elif mode == "WALKING":
        html_inst = step.get("html_instructions", "")
        # Strip HTML tags for summary
        import re
        summary = re.sub(r"<[^>]+>", "", html_inst) if html_inst else "Walk"
        if not summary:
            summary = "Walk"
    elif mode == "DRIVING":
        summary = "Drive"
    elif mode == "BICYCLING":
        summary = "Bicycle"

    return TransitLeg(
        mode=mode,
        system=system,
        line=line,
        headsign=headsign,
        depart_at=depart_at,
        arrive_at=arrive_at,
        duration_seconds=duration_sec,
        summary=summary,
        departure_stop=departure_stop_name,
        arrival_stop=arrival_stop_name,
    )


def _parse_route(response: dict[str, Any]) -> Optional[Route]:
    """Parse a Google Directions API response into a Route.

    Returns None if no valid routes found.
    """
    status = response.get("status", "")
    if status != "OK":
        return None

    routes = response.get("routes", [])
    if not routes:
        return None

    best_route = None
    best_score = math.inf

    for route_index, route in enumerate(routes):
        legs = route.get("legs", [])
        if not legs:
            continue

        transit_legs: list[TransitLeg] = []
        total_walk_seconds = 0
        transfers = 0
        prev_was_transit = False

        for leg in legs:
            steps = leg.get("steps", [])
            for step in steps:
                transit_leg = _parse_step(step, NYC_TZ)
                if transit_leg is None:
                    continue

                transit_legs.append(transit_leg)

                if transit_leg.mode == "WALKING":
                    total_walk_seconds += transit_leg.duration_seconds
                elif transit_leg.mode == "TRANSIT":
                    if prev_was_transit:
                        transfers += 1
                    prev_was_transit = True
                else:
                    prev_was_transit = False

        if not transit_legs:
            continue

        # Get overall departure/arrival times from first/last leg
        first_leg = legs[0]
        last_leg = legs[-1]

        dep_time = first_leg.get("departure_time", {})
        arr_time = last_leg.get("arrival_time", {})

        if isinstance(dep_time, dict):
            dep_ts = dep_time.get("value")
            depart_at = datetime.fromtimestamp(dep_ts, tz=NYC_TZ) if dep_ts else datetime.now(NYC_TZ)
        else:
            depart_at = datetime.now(NYC_TZ)

        if isinstance(arr_time, dict):
            arr_ts = arr_time.get("value")
            arrive_at = datetime.fromtimestamp(arr_ts, tz=NYC_TZ) if arr_ts else datetime.now(NYC_TZ)
        else:
            arrive_at = datetime.now(NYC_TZ)

        # Prefer elapsed door-to-door time; without endpoints, use complete leg
        # durations. Retain the old zero placeholder internally when unknown;
        # route_timing excludes it from exports and scheduling.
        total_duration = sum(leg.get("duration", {}).get("value", 0) for leg in legs)
        timing = _provider_timing(route)
        if timing.duration_seconds is not None:
            total_duration = int(timing.duration_seconds)
        fare = route.get("fare", {})
        fare_cents = None
        if fare:
            fare_value = fare.get("value")
            if fare_value is not None:
                # Convert to cents, assuming fare is in the local currency
                fare_cents = int(fare_value * 100)

        route_obj = Route(
            legs=transit_legs,
            depart_at=depart_at,
            arrive_at=arrive_at,
            total_duration_seconds=total_duration,
            transfers=transfers,
            fare_estimate_cents=fare_cents,
            raw_provider_payload=response,
            provider_route_index=route_index,
        )

        # Score route: minimize total_walk + 0.5 * transfers + 0.1 * duration
        score = total_walk_seconds + 0.5 * transfers + 0.1 * total_duration

        if score < best_score:
            best_score = score
            best_route = route_obj

    return best_route


def plan_route(
    origin: Origin,
    destination: ResolvedLocation,
    arrival_time: datetime,
    mode: Literal["transit", "driving", "walking", "bicycling"] = "transit",
    api_key: str = "",
) -> Optional[Route]:
    """Plan a route from origin to destination.

    Calls Google Directions API and returns the best route based on scoring:
    minimize total_walk + 0.5 * transfers + 0.1 * duration.

    Returns None if no route found.
    """
    if not api_key:
        return None

    # Build origin string
    origin_str = f"{origin.lat},{origin.lon}"

    # destination.value can be an address or station name
    destination_str = destination.value

    # Build request params
    params: dict[str, str] = {
        "origin": origin_str,
        "destination": destination_str,
        "arrival_time": str(_unix(arrival_time)),
        "mode": mode,
        "key": api_key,
    }

    # Add transit-specific params
    if mode == "transit":
        params["transit_mode"] = "subway|train|bus"

    params["alternatives"] = "true"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params=params,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError:
        return None

    if not isinstance(data, dict):
        return None
    return _parse_route(data)
