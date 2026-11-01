"""Fail-closed Directions → static GTFS boarding identity join.

``validate_leg`` never mutates its input. A successful result contains a copied
leg with complete identity, provenance and the *static* departure instant. A
rejection contains no leg and a stable reason code. Index/IO errors deliberately
propagate to routing's fail-open boundary, where they are logged as warnings.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from commutecompass.gtfs_allowlist import (
    haversine_m,
    load_allowlist,
    normalize_line,
    normalize_name,
)
from commutecompass.gtfs_index import GtfsIndex, rt_trip_id, service_instant
from commutecompass.models import GtfsProvenance, TransitLeg
from commutecompass.timeutil import NYC_TZ

RejectionReason = Literal[
    "untrusted_schedule", "unsupported_vehicle", "unsupported_agency",
    "route_not_allowlisted", "station_not_allowlisted", "station_coordinates",
    "ambiguous_station", "outside_coverage", "no_departure", "ambiguous_departure",
    "direction_mismatch", "invalid_rt_trip_id", "duplicate_rt_trip_id",
    "repeated_stop", "invalid_num_stops", "arrival_stop_missing",
    "arrival_station_mismatch", "arrival_time_mismatch",
]


@dataclass(frozen=True)
class ValidationResult:
    leg: TransitLeg | None = None
    reason: RejectionReason | None = None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _timestamp(value: object) -> datetime | None:
    seconds = _dict(value).get("value")
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _coordinates_match(stop: object, lat: float, lon: float, tolerance_m: float) -> bool:
    location = _dict(_dict(stop).get("location"))
    y, x = location.get("lat"), location.get("lng")
    if (isinstance(y, bool) or not isinstance(y, (int, float))
            or isinstance(x, bool) or not isinstance(x, (int, float))):
        return False
    return (math.isfinite(y) and math.isfinite(x) and -90 <= y <= 90 and -180 <= x <= 180
            and haversine_m(y, x, lat, lon) <= tolerance_m)


def validate_leg(
    leg: TransitLeg, payload_step: dict[str, Any], index: GtfsIndex,
    *, now: datetime | None = None,
) -> ValidationResult:
    """Validate one fresh subway leg, or return its precise rejection reason.

    ``now`` only controls provenance; the caller owns index freshness via
    ``GtfsIndex.open``. Candidate service days are the boarding NYC date and
    its predecessor. All schedule comparisons use absolute UTC instants.
    """
    details = _dict(payload_step.get("transit_details"))
    departure = _timestamp(details.get("departure_time"))
    if (leg.mode != "TRANSIT" or not leg.scheduled_departure_valid or departure is None
            or leg.depart_at.utcoffset() is None
            or leg.depart_at.astimezone(UTC) != departure):
        return ValidationResult(reason="untrusted_schedule")
    line = _dict(details.get("line"))
    if _dict(line.get("vehicle")).get("type") != "SUBWAY" or leg.system != "MTA Subway":
        return ValidationResult(reason="unsupported_vehicle")
    agencies = line.get("agencies")
    if not isinstance(agencies, list) or not any(
        "mta new york city transit" in _text(_dict(a).get("name")).casefold()
        for a in agencies
    ):
        return ValidationResult(reason="unsupported_agency")
    line_name = normalize_line(_text(line.get("short_name") or line.get("name")))
    entries = [e for e in load_allowlist() if line_name in e.directions_line
               and line_name in e.route_ids]
    if not entries:
        return ValidationResult(reason="route_not_allowlisted")
    boarding = _dict(details.get("departure_stop"))
    name = normalize_name(_text(boarding.get("name")))
    entries = [e for e in entries if name in {normalize_name(n) for n in e.directions_names}]
    if not entries:
        return ValidationResult(reason="station_not_allowlisted")
    stations = []
    for entry in entries:
        parent = index.stop(entry.parent_station)
        if parent is not None and parent.location_type == 1 and _coordinates_match(
            boarding, parent.stop_lat, parent.stop_lon, entry.max_coord_distance_m,
        ):
            stations.append(entry)
    if not stations:
        return ValidationResult(reason="station_coordinates")
    if len(stations) != 1:
        return ValidationResult(reason="ambiguous_station")
    entry = stations[0]
    # The index accounts for long GTFS offsets and DST anchors; do not prune
    # its service dates before checking ambiguity (even the next day can match).
    candidates = index.departures_near(
        entry.platform_ids, (line_name,), departure, window_s=30,
    )
    if not candidates:
        local_date = departure.astimezone(NYC_TZ).date()
        if not any(index.covers(day) for day in (local_date, local_date - timedelta(days=1))):
            return ValidationResult(reason="outside_coverage")
        return ValidationResult(reason="no_departure")
    # Do not let the arrival/headsign checks break ties between boardings.
    if len(candidates) != 1:
        return ValidationResult(reason="ambiguous_departure")
    match = candidates[0]
    rt_id = rt_trip_id(match.trip_id)
    if not rt_id or match.rt_trip_id != rt_id:
        return ValidationResult(reason="invalid_rt_trip_id")
    direction = "N" if match.direction_id == 0 else "S" if match.direction_id == 1 else None
    trip_shape = re.search(r"\.{1,2}([NS])", rt_id)
    static_shape = re.search(r"\.{1,2}([NS])", match.shape_id or "")
    if (direction is None or match.stop_id != entry.platforms[direction]
            or trip_shape is None or trip_shape[1] != direction
            or static_shape is None or static_shape[1] != direction):
        return ValidationResult(reason="direction_mismatch")
    if not index.rt_trip_id_unique(rt_id, match.service_date):
        return ValidationResult(reason="duplicate_rt_trip_id")
    stops = index.trip_stops(match.trip_id)
    if len({s.stop_id for s in stops}) != len(stops):
        return ValidationResult(reason="repeated_stop")
    if "num_stops" in details:
        count = details["num_stops"]
        if type(count) is not int or count <= 0:
            return ValidationResult(reason="invalid_num_stops")
        alighting = [s for s in stops if s.stop_sequence == match.stop_sequence + count]
        if len(alighting) != 1:
            return ValidationResult(reason="arrival_stop_missing")
        stop = alighting[0]
        parent = index.stop(stop.parent_station or stop.stop_id)
        arrival_stop = _dict(details.get("arrival_stop"))
        if (parent is None or normalize_name(_text(arrival_stop.get("name")))
                != normalize_name(parent.stop_name) or not _coordinates_match(
                    arrival_stop, parent.stop_lat, parent.stop_lon, entry.max_coord_distance_m,
                )):
            return ValidationResult(reason="arrival_station_mismatch")
        arrival = _timestamp(details.get("arrival_time"))
        if arrival != service_instant(match.service_date, stop.arrival_s):
            return ValidationResult(reason="arrival_time_mismatch")
    info = index.feed_info()
    return ValidationResult(leg=leg.model_copy(update={
        "depart_at": match.departure_utc.astimezone(NYC_TZ),
        "gtfs_trip_id": rt_id, "gtfs_route_id": match.route_id,
        "gtfs_start_date": match.service_date.strftime("%Y%m%d"), "gtfs_start_time": None,
        "gtfs_boarding_stop_id": match.stop_id,
        "gtfs_boarding_stop_sequence": match.stop_sequence,
        "gtfs_direction_id": match.direction_id,
        "gtfs_provenance": GtfsProvenance(
            source="gtfs_bridge", feed_kind=info.feed_kind, feed_version=info.feed_version,
            feed_sha256=info.feed_sha256, validated_at=(now or datetime.now(UTC)).astimezone(UTC),
        ),
    }))
