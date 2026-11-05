"""Google Directions routing."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple

import httpx

from commutecompass.gtfs_bridge import validate_leg
from commutecompass.gtfs_index import GtfsIndex
from commutecompass.models import Origin, ResolvedLocation, Route, TransitLeg
from commutecompass.timeutil import NYC_TZ

logger = logging.getLogger(__name__)


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

    duration_seconds: float | None = None
    depart_at: datetime | None = None
    arrive_at: datetime | None = None


def _provider_time(value: object) -> datetime | None:
    # Overall endpoints and nested boarding times share placeholder validation.
    return _scheduled_time(value, NYC_TZ)


def _provider_timing(candidate: object) -> RouteTiming:
    if not isinstance(candidate, dict):
        return RouteTiming()
    legs = candidate.get("legs")
    if not isinstance(legs, list):
        return RouteTiming()
    # Match the parser's treatment of malformed leg containers.
    legs = [leg for leg in legs if isinstance(leg, dict)]
    if not legs:
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
    try:
        seconds_total = float(sum(durations))
        if not math.isfinite(seconds_total):
            return RouteTiming(None, depart, arrive)
    except OverflowError:
        return RouteTiming(None, depart, arrive)
    return RouteTiming(seconds_total, depart, arrive)


def route_timing(route: Route | None) -> RouteTiming:
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
            elif (depart is None or arrive is None) and (
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
) -> Route | None:
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


def _scheduled_time(value: Any, nyc_tz: Any) -> datetime | None:
    """Read an explicit Directions timestamp, rejecting missing/placeholders."""
    if not isinstance(value, dict):
        return None
    ts = value.get("value")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        if not math.isfinite(ts) or ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=nyc_tz)
    except (ValueError, OverflowError, OSError):
        return None


def _display_endpoints(
    timing: RouteTiming, request_time: datetime | None,
) -> tuple[datetime, datetime] | None:
    """Derive display-only endpoints without inventing a provider schedule."""
    duration = timing.duration_seconds
    if duration is None:
        return None
    depart, arrive = timing.depart_at, timing.arrive_at
    try:
        if depart is not None and arrive is not None:
            return depart, arrive
        delta = timedelta(seconds=duration)
        if depart is not None:
            arrive = (depart.astimezone(UTC) + delta).astimezone(NYC_TZ)
        else:
            arrive = arrive or request_time or datetime.fromtimestamp(0, tz=NYC_TZ)
            if arrive.utcoffset() is None:
                return None
            depart = (arrive.astimezone(UTC) - delta).astimezone(NYC_TZ)
        return depart, arrive
    except (ValueError, OverflowError, OSError):
        return None


def _parse_step(
    step: dict[str, Any], nyc_tz: Any, fallback_at: datetime | None = None,
) -> TransitLeg | None:
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
    departure_time = step.get("departure_time", {}) if mode != "TRANSIT" else {}
    arrival_time = step.get("arrival_time", {}) if mode != "TRANSIT" else {}

    # Non-transit steps frequently omit times. Anchor them to the surrounding
    # schedule, never to the clock while parsing a response.
    scheduled_departure = _scheduled_time(departure_time, nyc_tz)
    scheduled_arrival = _scheduled_time(arrival_time, nyc_tz)
    anchor = (
        datetime.now(nyc_tz) if mode == "TRANSIT"
        else fallback_at or datetime.fromtimestamp(0, tz=nyc_tz)
    )
    depart_at = scheduled_departure or (
        datetime.fromtimestamp(scheduled_arrival.timestamp() - duration_sec, tz=nyc_tz)
        if scheduled_arrival is not None else anchor
    )
    arrive_at = scheduled_arrival or datetime.fromtimestamp(
        depart_at.timestamp() + duration_sec, tz=nyc_tz,
    )

    scheduled_departure_valid = False
    transit_details = step.get("transit_details", {})
    if not isinstance(transit_details, dict):
        transit_details = {}
    if mode == "TRANSIT":
        # Transit step times live under transit_details, not on the step.
        # Keep a display/planning fallback, but never treat it as a schedule.
        scheduled_departure = _scheduled_time(transit_details.get("departure_time"), nyc_tz)
        scheduled_arrival = _scheduled_time(transit_details.get("arrival_time"), nyc_tz)
        scheduled_departure_valid = scheduled_departure is not None
        depart_at = scheduled_departure or datetime.now(nyc_tz)
        arrive_at = scheduled_arrival or datetime.now(nyc_tz)

    system: str | None = None
    line: str | None = None
    headsign: str | None = None
    departure_stop_name: str | None = None
    arrival_stop_name: str | None = None
    summary = ""

    if mode == "TRANSIT":
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
        scheduled_departure_valid=scheduled_departure_valid,
    )


def _attach_bridge_identity(route: Route, gtfs_db_path: str) -> Route:
    """Enrich only the selected route's first boarding; every failure is safe."""
    first = next((i for i, leg in enumerate(route.legs) if leg.mode == "TRANSIT"), None)
    if first is None:
        return route
    try:
        index = GtfsIndex.open(gtfs_db_path)
        if index is None:
            logger.info("GTFS bridge: index unavailable (missing, stale or invalid): %s", gtfs_db_path)
            return route
        with index:
            payload = route.raw_provider_payload
            assert payload is not None and route.provider_route_index is not None
            candidate = payload["routes"][route.provider_route_index]
            step = next(step for leg in candidate["legs"] for step in leg.get("steps", [])
                        if step.get("travel_mode", "").upper() == "TRANSIT")
            result = validate_leg(route.legs[first], step, index)
            if result.leg is None:
                logger.info("GTFS bridge rejected boarding: %s", result.reason)
                return route
            updated = list(route.legs)
            updated[first] = result.leg
        return route.model_copy(update={"legs": updated})
    except Exception:
        # Neither database errors nor validator defects may break a commute.
        logger.warning("GTFS bridge failed; keeping Directions route unchanged", exc_info=True)
        return route


def _parse_route(
    response: dict[str, Any], *, gtfs_db_path: str = "", request_time: datetime | None = None,
) -> Route | None:
    """Parse a Google Directions API response into a Route.

    Returns None if no valid routes found.
    """
    status = response.get("status", "")
    if status != "OK":
        return None

    routes = response.get("routes", [])
    if not isinstance(routes, list) or not routes:
        return None

    best_route = None
    best_score = math.inf

    for route_index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        raw_legs = route.get("legs", [])
        if not isinstance(raw_legs, list):
            continue
        legs = [leg for leg in raw_legs if isinstance(leg, dict)]
        if not legs:
            continue

        timing = _provider_timing(route)
        display = _display_endpoints(timing, request_time)
        if display is None:
            continue
        depart_at, arrive_at = display
        assert timing.duration_seconds is not None
        total_duration = int(timing.duration_seconds)

        transit_legs: list[TransitLeg] = []
        total_walk_seconds = 0
        transfers = 0
        prev_was_transit = False

        for leg in legs:
            fallback_at = (
                _scheduled_time(leg.get("departure_time"), NYC_TZ)
                or (transit_legs[-1].arrive_at if transit_legs else depart_at)
            )
            steps = leg.get("steps", [])
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                transit_leg = _parse_step(step, NYC_TZ, fallback_at)
                if transit_leg is None:
                    continue

                fallback_at = transit_leg.arrive_at
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

        # Overall display times were derived only from this candidate's validated
        # endpoints/duration, never from nested transit processing-time placeholders.
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

    if best_route is not None:
        return _attach_bridge_identity(best_route, gtfs_db_path)
    return None


def plan_route(
    origin: Origin,
    destination: ResolvedLocation,
    arrival_time: datetime,
    mode: Literal["transit", "driving", "walking", "bicycling"] = "transit",
    api_key: str = "",
    *,
    gtfs_db_path: str = "",
) -> Route | None:
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
        if not isinstance(data, dict):
            return None
        return _parse_route(data, gtfs_db_path=gtfs_db_path, request_time=arrival_time)
    except httpx.HTTPError:
        return None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError) as exc:
        logger.warning("Directions response invalid: %s", type(exc).__name__)
        return None
