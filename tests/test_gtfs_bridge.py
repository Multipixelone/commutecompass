"""Static identity is all-or-nothing, unique, and instant-based."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest.mock import patch

import pytest

from bridge_helpers import BOARDING, DAY, NOW, TRIP, build_index, open_index, transit_step
from commutecompass.gtfs_allowlist import load_allowlist
from commutecompass.gtfs_bridge import RejectionReason, validate_leg
from commutecompass.gtfs_index import GtfsIndex, service_instant
from commutecompass.models import TransitLeg
from commutecompass.routing import _parse_step
from commutecompass.timeutil import NYC_TZ


@pytest.fixture
def index(tmp_path: Path) -> Iterator[GtfsIndex]:
    with open_index(build_index(tmp_path / "gtfs.db")) as idx:
        yield idx


def parsed(step: dict[str, Any]) -> TransitLeg:
    leg = _parse_step(step, NYC_TZ)
    assert leg is not None
    return leg


def test_success_and_static_departure_anchor(index: GtfsIndex) -> None:
    step = transit_step()
    step["transit_details"]["departure_time"]["value"] += 30
    leg = parsed(step)
    result = validate_leg(leg, step, index, now=NOW)
    assert result.reason is None and result.leg is not None
    assert leg.gtfs_provenance is None and leg.depart_at != BOARDING
    matched = result.leg
    assert matched.depart_at.astimezone(UTC) == BOARDING
    assert matched.gtfs_trip_id == "050800_A..N"
    assert matched.gtfs_route_id == "A"
    assert matched.gtfs_start_date == "20261030" and matched.gtfs_start_time is None
    assert matched.gtfs_boarding_stop_id == "A48N"
    assert matched.gtfs_boarding_stop_sequence == 10 and matched.gtfs_direction_id == 0
    assert matched.gtfs_provenance is not None
    assert matched.gtfs_provenance.validated_at == NOW
    assert matched.gtfs_provenance.feed_sha256 == index.feed_info().feed_sha256


# Every public rejection code has a direct regression. DB APIs are patched only
# for conditions an importer normally excludes (e.g. repeated stops/corruption).
@pytest.mark.parametrize("reason", get_args(RejectionReason))
def test_rejection_reasons(index: GtfsIndex, reason: RejectionReason) -> None:
    step = transit_step()
    details = step["transit_details"]
    leg = parsed(step)
    match = index.departures_near(("A48N",), ("A",), BOARDING)[0]
    patches: list[Any] = []
    if reason == "untrusted_schedule":
        leg.scheduled_departure_valid = False
    elif reason == "unsupported_vehicle":
        details["line"]["vehicle"]["type"] = "BUS"
    elif reason == "unsupported_agency":
        details["line"]["agencies"] = [{"name": "Other Subway"}]
    elif reason == "route_not_allowlisted":
        details["line"]["short_name"] = "AX"
    elif reason == "station_not_allowlisted":
        details["departure_stop"]["name"] = "Other Utica Av"
    elif reason == "station_coordinates":
        details["departure_stop"]["location"]["lat"] = 40.75
    elif reason == "ambiguous_station":
        entry = load_allowlist()[0]
        patches.append(patch("commutecompass.gtfs_bridge.load_allowlist", return_value=(entry, entry)))
    elif reason == "outside_coverage":
        patches.append(patch.object(index, "covers", return_value=False))
    elif reason == "no_departure":
        patches.append(patch.object(index, "departures_near", return_value=[]))
    elif reason == "ambiguous_departure":
        patches.append(patch.object(index, "departures_near", return_value=[match, match]))
    elif reason == "invalid_rt_trip_id":
        patches.append(patch.object(index, "departures_near", return_value=[replace(match, rt_trip_id=None)]))
    elif reason == "direction_mismatch":
        patches.append(patch.object(index, "departures_near", return_value=[replace(match, direction_id=1)]))
    elif reason == "duplicate_rt_trip_id":
        patches.append(patch.object(index, "rt_trip_id_unique", return_value=False))
    elif reason == "repeated_stop":
        stops = index.trip_stops(TRIP)
        patches.append(patch.object(index, "trip_stops", return_value=stops + stops))
    elif reason == "invalid_num_stops":
        details["num_stops"] = True
    elif reason == "arrival_stop_missing":
        details["num_stops"] = 2
    elif reason == "arrival_station_mismatch":
        details["arrival_stop"]["location"]["lng"] = -74.0
    elif reason == "arrival_time_mismatch":
        details["arrival_time"]["value"] += 1
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = validate_leg(leg, step, index)
    assert result.leg is None and result.reason == reason
    assert leg.gtfs_provenance is None


@pytest.mark.parametrize("field,value", [("lat", None), ("lat", True), ("lat", float("nan")),
                                         ("lng", 200), ("lng", "-73.93")])
def test_bad_coordinates(index: GtfsIndex, field: str, value: object) -> None:
    step = transit_step()
    step["transit_details"]["departure_stop"]["location"][field] = value
    assert validate_leg(parsed(step), step, index).reason == "station_coordinates"


@pytest.mark.parametrize("offset", [-31, 31])
def test_departure_window_is_bounded(index: GtfsIndex, offset: int) -> None:
    step = transit_step(BOARDING + timedelta(seconds=offset))
    assert validate_leg(parsed(step), step, index).reason == "no_departure"


def test_num_stops_optional_and_line_name_fallback(index: GtfsIndex) -> None:
    step = transit_step()
    del step["transit_details"]["num_stops"]
    step["transit_details"]["line"].update(short_name="", name=" a ")
    assert validate_leg(parsed(step), step, index).leg is not None


@pytest.mark.parametrize("day,offset", [
    (DAY, 24 * 3600 + 30 * 60),
    (date(2026, 10, 31), 25 * 3600 + 30 * 60),
    (date(2026, 11, 1), 90 * 60),
    (date(2027, 3, 14), 150 * 60),
])
def test_service_day_and_dst(tmp_path: Path, day: date, offset: int) -> None:
    boarding = service_instant(day, offset)
    h, rem = divmod(offset, 3600)
    m, s = divmod(rem, 60)
    time1, time2 = f"{h:02}:{m:02}:{s:02}", f"{h:02}:{m + 2:02}:{s:02}"
    files = {"stop_times.txt": "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
             f"{TRIP},A48N,{time1},{time1},10\n{TRIP},A47N,{time2},{time2},11\n"}
    now = boarding - timedelta(minutes=15)
    with open_index(build_index(tmp_path / "gtfs.db", files=files, now=now), now=now) as idx:
        step = transit_step(boarding)
        result = validate_leg(parsed(step), step, idx, now=now)
        assert result.leg is not None
        assert result.leg.gtfs_start_date == day.strftime("%Y%m%d")
        assert result.leg.depart_at.astimezone(UTC) == boarding


def test_removed_service(tmp_path: Path) -> None:
    files = {"calendar_dates.txt": "service_id,date,exception_type\nWeekday,20261030,2\n"}
    with open_index(build_index(tmp_path / "gtfs.db", files=files)) as idx:
        step = transit_step()
        assert validate_leg(parsed(step), step, idx).reason == "no_departure"


def test_duplicate_suffix_across_active_services(tmp_path: Path) -> None:
    files = {"trips.txt": "route_id,trip_id,service_id,direction_id,shape_id\n"
             f"A,{TRIP},Weekday,0,A..N\nA,PICK_050800_A..N,Pick,0,A..N\n",
             "calendar_dates.txt": "service_id,date,exception_type\nPick,20261030,1\n"}
    with open_index(build_index(tmp_path / "gtfs.db", files=files)) as idx:
        step = transit_step()
        assert validate_leg(parsed(step), step, idx).reason == "duplicate_rt_trip_id"


def test_actual_ambiguous_boarding(tmp_path: Path) -> None:
    files = {"trips.txt": "route_id,trip_id,service_id,direction_id,shape_id\n"
             f"A,{TRIP},Weekday,0,A..N\nA,WK_050801_A..N,Weekday,0,A..N\n",
             "stop_times.txt": "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
             f"{TRIP},A48N,08:30:00,08:30:00,10\n"
             "WK_050801_A..N,A48N,08:30:01,08:30:01,10\n"}
    with open_index(build_index(tmp_path / "gtfs.db", files=files)) as idx:
        step = transit_step()
        assert validate_leg(parsed(step), step, idx).reason == "ambiguous_departure"
