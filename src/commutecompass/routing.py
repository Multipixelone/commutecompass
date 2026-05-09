"""Google Directions routing."""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Literal, Optional

import httpx

from commutecompass.models import Origin, ResolvedLocation, Route, TransitLeg
from commutecompass.timeutil import NYC_TZ


def _unix(dt: datetime) -> int:
    """Convert datetime to Unix timestamp."""
    return int(dt.timestamp())


def _parse_step(step: dict, nyc_tz: type) -> Optional[TransitLeg]:
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
    )


def _parse_route(response: dict) -> Optional[Route]:
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

    for route in routes:
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

        # Sum leg durations for total (primary approach for legacy Directions schema)
        total_duration = sum(leg.get("duration", {}).get("value", 0) for leg in legs)
        fare = route.get("fare", {})
        fare_cents = None
        if fare:
            fare_value = fare.get("value")
            currency = fare.get("currency", "USD")
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

    return _parse_route(data)
