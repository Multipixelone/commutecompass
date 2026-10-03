"""Tests for routing.py — Google Directions parsing and route scoring."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from commutecompass.models import Origin, ResolvedLocation, Route
from commutecompass.config import RealtimeConfig
from commutecompass.realtime import FetchResult, Predictions, _accumulate, realtime_delay
from commutecompass.store import Store
from commutecompass.routing import (
    _parse_route,
    _unix,
    estimate_route,
    plan_route,
    route_cache_key,
    route_timing,
)
from commutecompass.timeutil import NYC_TZ


# ─── Transfer counting ─────────────────────────────────────────────────────────

def _transit_step(line: str, dur: int = 1500) -> dict[str, Any]:
    return {
        "travel_mode": "TRANSIT",
        "duration": {"value": dur},
        "transit_details": {
            "line": {"short_name": line, "vehicle": {"type": "SUBWAY"}},
            "departure_stop": {"name": "A"},
            "arrival_stop": {"name": "B"},
        },
    }


def _walk_step(dur: int = 300) -> dict[str, Any]:
    return {"travel_mode": "WALKING", "duration": {"value": dur}}


def _route_from_steps(steps: list[dict[str, Any]]) -> Route:
    resp = {
        "status": "OK",
        "routes": [
            {
                "legs": [
                    {
                        "departure_time": {"value": 1000},
                        "arrival_time": {"value": 5000},
                        "duration": {"value": 4000},
                        "steps": steps,
                    }
                ]
            }
        ],
    }
    route = _parse_route(resp)
    assert route is not None
    return route


def test_transfer_count_includes_walking_transfer() -> None:
    """A walk between two trains is still one transfer, not zero."""
    route = _route_from_steps(
        [_walk_step(), _transit_step("A"), _walk_step(), _transit_step("C"), _walk_step()]
    )
    assert route.transfers == 1


def test_transfer_count_single_train_is_zero() -> None:
    route = _route_from_steps([_walk_step(), _transit_step("A"), _walk_step()])
    assert route.transfers == 0


# ─── Fallback estimate ─────────────────────────────────────────────────────────

def test_route_cache_key_rounds_coordinates() -> None:
    a = Origin(address="a", lat=40.69501, lon=-73.98904)
    b = Origin(address="b", lat=40.69499, lon=-73.98897)  # within ~11m
    assert route_cache_key(a) == route_cache_key(b)
    far = Origin(address="c", lat=40.75, lon=-73.99)
    assert route_cache_key(far) != route_cache_key(a)


def test_estimate_route_produces_approximate_route() -> None:
    origin = Origin(address="home", lat=40.6950, lon=-73.9890)
    dest = ResolvedLocation(
        kind="address", value="Midtown", lat=40.7549, lon=-73.9840, source="geocode"
    )
    arrival = datetime(2026, 5, 8, 14, 30, tzinfo=NYC_TZ)

    route = estimate_route(origin, dest, arrival, "transit")
    assert route is not None
    assert route.approximate is True
    assert route.total_duration_seconds > 0
    # arrive_at is the requested time; depart_at precedes it by the estimate.
    assert route.arrive_at == arrival
    assert route.depart_at < arrival


def test_estimate_route_none_without_destination_coords() -> None:
    origin = Origin(address="home", lat=40.6950, lon=-73.9890)
    dest = ResolvedLocation(kind="station", value="Somewhere LIRR", source="llm")
    arrival = datetime(2026, 5, 8, 14, 30, tzinfo=NYC_TZ)
    assert estimate_route(origin, dest, arrival, "transit") is None


def test_estimate_route_preserves_duration_across_dst() -> None:
    origin = Origin(address="Home", lat=40.6950, lon=-73.9890)
    dest = ResolvedLocation(
        kind="address", value="Work", lat=40.7549, lon=-73.9840, source="geocode",
    )
    arrival = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=1)
    route = estimate_route(origin, dest, arrival)
    assert route is not None
    assert route.arrive_at.timestamp() - route.depart_at.timestamp() == route.total_duration_seconds
    assert route_timing(route).duration_seconds == route.total_duration_seconds


def test_estimate_route_slower_modes_take_longer() -> None:
    origin = Origin(address="home", lat=40.6950, lon=-73.9890)
    dest = ResolvedLocation(
        kind="address", value="Midtown", lat=40.7549, lon=-73.9840, source="geocode"
    )
    arrival = datetime(2026, 5, 8, 14, 30, tzinfo=NYC_TZ)
    walking = estimate_route(origin, dest, arrival, "walking")
    driving = estimate_route(origin, dest, arrival, "driving")
    assert walking is not None and driving is not None
    assert walking.total_duration_seconds > driving.total_duration_seconds


@pytest.mark.parametrize("legacy", [False, True])
def test_selected_route_timing(scheduled_directions: dict[str, Any], legacy: bool) -> None:
    route = _parse_route(scheduled_directions)
    assert route is not None
    assert route.provider_route_index == 2
    if legacy:
        # Old JSON has no selected index and parses dates with fixed offsets.
        data = route.model_dump(mode="json", exclude={"provider_route_index"})
        route = Route.model_validate(data)
    timing = route_timing(route)
    assert timing.duration_seconds == 3095
    assert timing.depart_at == datetime(2026, 10, 3, 13, 49, 8, tzinfo=NYC_TZ)
    assert timing.arrive_at == datetime(2026, 10, 3, 14, 40, 43, tzinfo=NYC_TZ)


def test_route_duration_uses_actual_elapsed_time(scheduled_directions: dict[str, Any]) -> None:
    # Leg duration totals can omit inter-leg waits or disagree with endpoints.
    scheduled_directions["routes"][2]["legs"][0]["duration"]["value"] = 9999
    route = _parse_route(scheduled_directions)
    assert route is not None
    assert route.provider_route_index == 2
    assert route.total_duration_seconds == 3095
    assert route_timing(route).duration_seconds == 3095


def test_route_times_not_borrowed_from_other_alternatives(
    scheduled_directions: dict[str, Any],
) -> None:
    route = _parse_route(scheduled_directions)
    assert route is not None
    selected = scheduled_directions["routes"][2]["legs"][0]
    del selected["arrival_time"]
    del selected["departure_time"]
    # Other alternatives still have the arrival stored on this route. They
    # cannot supply timestamps for the now-untimed selected alternative.
    timing = route_timing(route)
    assert timing.duration_seconds == 3095
    assert timing.depart_at is None and timing.arrive_at is None


def test_ambiguous_legacy_route_has_no_schedule(scheduled_directions: dict[str, Any]) -> None:
    route = _parse_route(scheduled_directions)
    assert route is not None
    route.provider_route_index = None
    duplicate = deepcopy(scheduled_directions["routes"][2])
    del duplicate["legs"][0]["arrival_time"]
    scheduled_directions["routes"].append(duplicate)
    timing = route_timing(route)
    assert timing.duration_seconds == 3095
    assert timing.depart_at is None and timing.arrive_at is None


def test_legacy_route_timing_across_dst_fallback(scheduled_directions: dict[str, Any]) -> None:
    candidate = scheduled_directions["routes"][2]
    leg = candidate["legs"][0]
    depart = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0)
    arrive = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=1)
    leg["departure_time"]["value"] = int(depart.timestamp())
    leg["arrival_time"]["value"] = int(arrive.timestamp())
    leg["duration"]["value"] = 3600
    route = _parse_route({"status": "OK", "routes": [candidate]})
    assert route is not None
    route = Route.model_validate(route.model_dump(mode="json", exclude={"provider_route_index"}))
    timing = route_timing(route)
    assert timing.duration_seconds == 3600
    assert timing.depart_at is not None and timing.arrive_at is not None
    assert timing.depart_at.isoformat().endswith("-04:00")
    assert timing.arrive_at.isoformat().endswith("-05:00")


# ─── Helper fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def directions_sample_path(fixtures_dir: Path) -> Path:
    """Return path to the directions sample fixture."""
    return fixtures_dir / "directions_sample.json"


@pytest.fixture
def directions_sample_data(directions_sample_path: Path) -> dict[str, Any]:
    """Load the directions sample JSON as a dict."""
    from typing import cast
    with open(directions_sample_path) as f:
        return cast(dict[str, Any], json.load(f))


@pytest.fixture
def origin() -> Origin:
    """Return a sample Origin for routing tests."""
    return Origin(
        address="123 Example Ave, Brooklyn NY 11201",
        lat=40.6950,
        lon=-73.9890,
        subway_station="Jay St-MetroTech",
        lirr_station="Atlantic Terminal",
    )


@pytest.fixture
def destination() -> ResolvedLocation:
    """Return a sample destination."""
    return ResolvedLocation(
        kind="address",
        value="200 Example St, New York, NY 10001",
        lat=40.7120,
        lon=-73.9080,
        source="known_venues",
    )


@pytest.fixture
def arrival_time() -> datetime:
    """Return a fixed arrival time for testing."""
    return datetime(2025, 5, 12, 9, 30, 0, tzinfo=NYC_TZ)


# ─── Test _unix ────────────────────────────────────────────────────────────────

class TestUnix:
    """Tests for _unix helper."""

    def test_unix_converts_aware_datetime(self) -> None:
        dt = datetime(2025, 5, 12, 9, 30, 0, tzinfo=NYC_TZ)
        result = _unix(dt)
        assert isinstance(result, int)
        # Verify the timestamp is reasonable (May 2025 should be ~1.7 billion)
        assert 1_700_000_000 < result < 1_800_000_000

    def test_unix_naive_datetime(self) -> None:
        dt = datetime(2025, 5, 12, 9, 30, 0)
        result = _unix(dt)
        assert isinstance(result, int)


# ─── Test _parse_route ─────────────────────────────────────────────────────────

class TestParseRoute:
    """Tests for _parse_route parsing Google Directions payload."""

    def test_parses_valid_response(self, directions_sample_data: dict[str, Any]) -> None:
        """A valid directions response parses into a Route."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        assert isinstance(route, Route)
        assert len(route.legs) == 3  # walk, transit, walk
        assert route.transfers == 0
        assert route.total_duration_seconds == 1680
        assert route.fare_estimate_cents == 290  # $2.90

    def test_identifies_subway_leg(self, directions_sample_data: dict[str, Any]) -> None:
        """The subway leg is correctly identified."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        subway_legs = [leg for leg in route.legs if leg.mode == "TRANSIT"]
        assert len(subway_legs) == 1

        subway = subway_legs[0]
        assert subway.mode == "TRANSIT"
        assert subway.system == "MTA Subway"
        assert subway.line == "C"

    def test_identifies_walking_legs(self, directions_sample_data: dict[str, Any]) -> None:
        """Walking legs are correctly identified."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        walk_legs = [leg for leg in route.legs if leg.mode == "WALKING"]
        assert len(walk_legs) == 2
        for walk in walk_legs:
            assert walk.mode == "WALKING"

    def test_depart_and_arrive_times(self, directions_sample_data: dict[str, Any]) -> None:
        """Departure and arrival times are correctly parsed."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        assert route.depart_at.tzinfo is not None
        assert route.arrive_at.tzinfo is not None
        assert route.arrive_at > route.depart_at

    def test_raw_payload_stored(self, directions_sample_data: dict[str, Any]) -> None:
        """The raw provider payload is stored on the route."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        assert route.raw_provider_payload is not None
        assert route.raw_provider_payload["status"] == "OK"

    def test_empty_routes_returns_none(self) -> None:
        """An empty routes array returns None."""
        response = {"routes": [], "status": "OK"}
        assert _parse_route(response) is None

    def test_zero_routes_returns_none(self) -> None:
        """No routes key returns None."""
        response = {"status": "OK"}
        assert _parse_route(response) is None

    def test_non_ok_status_returns_none(self) -> None:
        """A non-OK status returns None."""
        response = {"routes": [{}], "status": "ZERO_RESULTS"}
        assert _parse_route(response) is None

    def test_zero_legs_returns_none(self) -> None:
        """A route with no legs returns None."""
        response = {"routes": [{"legs": []}], "status": "OK"}
        assert _parse_route(response) is None

    def test_transfers_counted_correctly(self) -> None:
        """Transfers are counted when moving between transit lines."""
        # Simulate a route with two transit legs (transfer)
        response: dict[str, Any] = {
            "routes": [
                {
                    "legs": [
                        {
                            "steps": [
                                {
                                    "travel_mode": "TRANSIT",
                                    "duration": {"value": 600},
                                    "departure_time": {"value": 1746864000},
                                    "arrival_time": {"value": 1746864600},
                                    "transit_details": {
                                        "line": {
                                            "name": "C",
                                            "vehicle": {"type": "SUBWAY"},
                                            "agencies": [{"name": "MTA NYC Transit"}],
                                        },
                                        "departure_stop": {"name": "Start"},
                                        "arrival_stop": {"name": "Transfer"},
                                    },
                                },
                                {
                                    "travel_mode": "TRANSIT",
                                    "duration": {"value": 600},
                                    "departure_time": {"value": 1746864600},
                                    "arrival_time": {"value": 1746865200},
                                    "transit_details": {
                                        "line": {
                                            "name": "A",
                                            "vehicle": {"type": "SUBWAY"},
                                            "agencies": [{"name": "MTA NYC Transit"}],
                                        },
                                        "departure_stop": {"name": "Transfer"},
                                        "arrival_stop": {"name": "End"},
                                    },
                                },
                            ],
                            "duration": {"value": 1200},
                            "departure_time": {"value": 1746864000},
                            "arrival_time": {"value": 1746865200},
                        }
                    ],
                    "duration": {"value": 1200},
                }
            ],
            "status": "OK",
        }
        route = _parse_route(response)
        assert route is not None
        assert route.transfers == 1

    def test_total_duration_from_legs_when_route_duration_missing(self) -> None:
        """total_duration_seconds is computed from leg durations when route-level duration is absent.

        Regression test: legacy Directions responses may not include route.duration,
        so we fall back to summing each leg's duration.value.
        """
        response: dict[str, Any] = {
            "routes": [
                {
                    "legs": [
                        {
                            "steps": [
                                {
                                    "travel_mode": "WALKING",
                                    "duration": {"value": 300},
                                    "departure_time": {"value": 1746864000},
                                    "arrival_time": {"value": 1746864300},
                                },
                            ],
                            "duration": {"value": 300},
                            "departure_time": {"value": 1746864000},
                            "arrival_time": {"value": 1746864300},
                        },
                        {
                            "steps": [
                                {
                                    "travel_mode": "TRANSIT",
                                    "duration": {"value": 1200},
                                    "departure_time": {"value": 1746864300},
                                    "arrival_time": {"value": 1746865500},
                                    "transit_details": {
                                        "line": {
                                            "name": "C",
                                            "vehicle": {"type": "SUBWAY"},
                                            "agencies": [{"name": "MTA NYC Transit"}],
                                        },
                                        "departure_stop": {"name": "Stop A"},
                                        "arrival_stop": {"name": "Stop B"},
                                    },
                                },
                            ],
                            "duration": {"value": 1200},
                            "departure_time": {"value": 1746864300},
                            "arrival_time": {"value": 1746865500},
                        },
                    ],
                    # Note: no "duration" key at route level
                }
            ],
            "status": "OK",
        }
        route = _parse_route(response)
        assert route is not None
        # Total should be sum of leg durations: 300 + 1200 = 1500
        assert route.total_duration_seconds == 1500

    def test_total_duration_from_legs_multi_leg_route(self) -> None:
        """Multi-leg route total_duration_seconds is sum of all leg durations."""
        response: dict[str, Any] = {
            "routes": [
                {
                    "legs": [
                        {
                            "steps": [
                                {
                                    "travel_mode": "WALKING",
                                    "duration": {"value": 180},
                                    "departure_time": {"value": 1746864000},
                                    "arrival_time": {"value": 1746864180},
                                },
                            ],
                            "duration": {"value": 180},
                            "departure_time": {"value": 1746864000},
                            "arrival_time": {"value": 1746864180},
                        },
                        {
                            "steps": [
                                {
                                    "travel_mode": "TRANSIT",
                                    "duration": {"value": 900},
                                    "departure_time": {"value": 1746864180},
                                    "arrival_time": {"value": 1746865080},
                                    "transit_details": {
                                        "line": {
                                            "name": "1",
                                            "vehicle": {"type": "SUBWAY"},
                                            "agencies": [{"name": "MTA NYC Transit"}],
                                        },
                                        "departure_stop": {"name": "A"},
                                        "arrival_stop": {"name": "B"},
                                    },
                                },
                            ],
                            "duration": {"value": 900},
                            "departure_time": {"value": 1746864180},
                            "arrival_time": {"value": 1746865080},
                        },
                        {
                            "steps": [
                                {
                                    "travel_mode": "WALKING",
                                    "duration": {"value": 120},
                                    "departure_time": {"value": 1746865080},
                                    "arrival_time": {"value": 1746865200},
                                },
                            ],
                            "duration": {"value": 120},
                            "departure_time": {"value": 1746865080},
                            "arrival_time": {"value": 1746865200},
                        },
                    ],
                    # No route-level duration
                }
            ],
            "status": "OK",
        }
        route = _parse_route(response)
        assert route is not None
        # 180 + 900 + 120 = 1200
        assert route.total_duration_seconds == 1200

    def test_prefers_short_name_over_name_for_line(self) -> None:
        """When both name and short_name are present, short_name takes precedence."""
        response: dict[str, Any] = {
            "routes": [
                {
                    "legs": [
                        {
                            "steps": [
                                {
                                    "travel_mode": "TRANSIT",
                                    "duration": {"value": 900},
                                    "departure_time": {"value": 1746864000},
                                    "arrival_time": {"value": 1746864900},
                                    "transit_details": {
                                        "line": {
                                            "name": "C Train (8 Av Local)",
                                            "short_name": "C",
                                            "vehicle": {"type": "SUBWAY"},
                                            "agencies": [{"name": "MTA NYC Transit"}],
                                        },
                                        "departure_stop": {"name": "Jay St-MetroTech"},
                                        "arrival_stop": {"name": "Fulton St"},
                                    },
                                },
                            ],
                            "duration": {"value": 900},
                            "departure_time": {"value": 1746864000},
                            "arrival_time": {"value": 1746864900},
                        }
                    ],
                    "duration": {"value": 900},
                }
            ],
            "status": "OK",
        }
        route = _parse_route(response)
        assert route is not None
        assert route.legs[0].line == "C"


# ─── Test plan_route ──────────────────────────────────────────────────────────

def _boarding_predictions(boarding: datetime, observed: datetime) -> FetchResult:
    from google.transit import gtfs_realtime_pb2 as gtfs  # type: ignore[import-untyped]

    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = int(observed.timestamp())
    entity = feed.entity.add()
    entity.id = "boarding"
    tu = entity.trip_update
    tu.timestamp = int(observed.timestamp())
    tu.trip.trip_id = "boarding"
    tu.trip.route_id = "C"
    tu.trip.start_date = boarding.strftime("%Y%m%d")
    tu.trip.direction_id = 0
    stu = tu.stop_time_update.add()
    stu.stop_id = "A41N"
    stu.stop_sequence = 10
    stu.departure.time = int((boarding + timedelta(minutes=6)).timestamp())
    stu.departure.delay = 360
    parsed = gtfs.FeedMessage.FromString(feed.SerializeToString())
    predictions: Predictions = {}
    usable = _accumulate(parsed, predictions, "MTA Subway", now=observed)
    return FetchResult(predictions, usable_feeds=int(usable))


def _add_boarding_identity(route: Route, boarding: datetime) -> None:
    # Independently validated context, NOT metadata invented by the parser.
    transit = route.legs[1]
    assert transit.gtfs_trip_id is None
    transit.gtfs_trip_id = "boarding"
    transit.gtfs_route_id = "C"
    transit.gtfs_start_date = boarding.strftime("%Y%m%d")
    transit.gtfs_boarding_stop_id = "A41N"
    transit.gtfs_boarding_stop_sequence = 10
    transit.gtfs_direction_id = 0


def test_nested_boarding_time_drives_realtime(directions_sample_data: dict[str, Any]) -> None:
    boarding = datetime.fromtimestamp(1746864300, tz=NYC_TZ)
    planning_time = boarding - timedelta(hours=2)
    with patch("commutecompass.routing.datetime") as clock:
        clock.now.return_value = planning_time
        clock.fromtimestamp.side_effect = datetime.fromtimestamp
        route = _parse_route(directions_sample_data)
    assert route is not None
    transit = route.legs[1]
    assert transit.depart_at == boarding
    assert transit.depart_at.tzinfo == NYC_TZ
    assert transit.arrive_at == datetime.fromtimestamp(1746865500, tz=NYC_TZ)
    assert transit.scheduled_departure_valid
    assert route.depart_at == datetime.fromtimestamp(1746864000, tz=NYC_TZ)
    assert route.arrive_at == datetime.fromtimestamp(1746865680, tz=NYC_TZ)

    _add_boarding_identity(route, boarding)

    def predictions(urls: list[str], system: str) -> FetchResult:
        return _boarding_predictions(boarding, planning_time)

    delay = realtime_delay(
        route, planning_time, RealtimeConfig(enabled=True), fetcher=predictions,
        clock=lambda: planning_time,
    )
    assert (delay.minutes, delay.reason, delay.status) == (6, "C running ~6 min late", "observed")


@pytest.mark.parametrize(
    "timing",
    ["missing", None, {}, {"text": "8:05 AM"}, {"value": None}, {"value": 0}, {"value": -1},
     {"value": True}, {"value": "unknown"}, {"value": float("nan")},
     {"value": float("inf")}, {"value": 1e30}, "placeholder"],
)
def test_invalid_boarding_time_fails_open(
    directions_sample_data: dict[str, Any], timing: Any
) -> None:
    step = directions_sample_data["routes"][0]["legs"][0]["steps"][1]
    step["transit_details"]["departure_time"] = timing
    step["transit_details"]["arrival_time"] = timing
    if timing == "missing":
        del step["transit_details"]["departure_time"]
        del step["transit_details"]["arrival_time"]
    # Incorrect step-level times must not rescue a missing boarding schedule.
    step["departure_time"] = {"value": 1746864300}
    planning_time = datetime.fromtimestamp(1746864300, tz=NYC_TZ) - timedelta(hours=2)
    with patch("commutecompass.routing.datetime") as clock:
        clock.now.return_value = planning_time
        clock.fromtimestamp.side_effect = datetime.fromtimestamp
        route = _parse_route(directions_sample_data)
    assert route is not None
    assert route.total_duration_seconds == 1680
    assert not route.legs[1].scheduled_departure_valid
    assert route.legs[1].depart_at == planning_time

    def no_fetch(urls: list[str], system: str) -> FetchResult:
        pytest.fail("Untrusted boarding time must not fetch realtime predictions")

    delay = realtime_delay(
        route, planning_time, RealtimeConfig(enabled=True), fetcher=no_fetch
    )
    assert delay.minutes == 0 and delay.status == "unmatched"


@pytest.mark.parametrize("legacy", [True, False])
def test_cached_boarding_timing_validity(
    directions_sample_data: dict[str, Any], tmp_path: Path, legacy: bool
) -> None:
    route = _parse_route(directions_sample_data)
    assert route is not None
    store = Store(tmp_path / "routes.sqlite")
    store.init_schema()
    boarding = route.legs[1].depart_at
    _add_boarding_identity(route, boarding)
    store.cache_route("origin", "destination", "transit", route)
    if legacy:
        payload = route.model_dump(mode="json")
        for leg in payload["legs"]:
            del leg["scheduled_departure_valid"]
        # Simulate the pre-fix parser's fabricated "now" timestamp.
        payload["legs"][1]["depart_at"] = (boarding - timedelta(hours=2)).isoformat()
        with store._connect() as conn:
            conn.execute("UPDATE route_cache SET route_json = ?", (json.dumps(payload),))
    cached = store.get_cached_route("origin", "destination", "transit")
    assert cached is not None
    assert cached.total_duration_seconds == route.total_duration_seconds
    assert cached.legs[1].scheduled_departure_valid is (not legacy)

    def predictions(urls: list[str], system: str) -> FetchResult:
        return _boarding_predictions(boarding, boarding)

    delay = realtime_delay(cached, boarding, RealtimeConfig(enabled=True), fetcher=predictions,
                           clock=lambda: boarding)
    assert delay.minutes == (0 if legacy else 6)

class TestPlanRoute:
    """Tests for plan_route function."""

    def test_plan_route_with_no_api_key_returns_none(
        self, origin: Origin, destination: ResolvedLocation, arrival_time: datetime
    ) -> None:
        """plan_route returns None when api_key is empty."""
        result = plan_route(origin, destination, arrival_time, api_key="")
        assert result is None

    def test_plan_route_accepts_all_modes(
        self, origin: Origin, destination: ResolvedLocation, arrival_time: datetime
    ) -> None:
        """plan_route accepts all supported mode values."""
        for mode in ("transit", "driving", "walking", "bicycling"):
            result = plan_route(origin, destination, arrival_time, mode=mode, api_key="")
            assert result is None  # No API key = None
