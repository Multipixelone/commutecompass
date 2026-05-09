"""Tests for routing.py — Google Directions parsing and route scoring."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from commutecompass.models import Origin, ResolvedLocation, Route, TransitLeg
from commutecompass.routing import _parse_route, _unix, plan_route
from commutecompass.timeutil import NYC_TZ


# ─── Helper fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def directions_sample_path(fixtures_dir: Path) -> Path:
    """Return path to the directions sample fixture."""
    return fixtures_dir / "directions_sample.json"


@pytest.fixture
def directions_sample_data(directions_sample_path: Path) -> dict:
    """Load the directions sample JSON as a dict."""
    with open(directions_sample_path) as f:
        return json.load(f)


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

    def test_parses_valid_response(self, directions_sample_data: dict) -> None:
        """A valid directions response parses into a Route."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        assert isinstance(route, Route)
        assert len(route.legs) == 3  # walk, transit, walk
        assert route.transfers == 0
        assert route.total_duration_seconds == 1680
        assert route.fare_estimate_cents == 290  # $2.90

    def test_identifies_subway_leg(self, directions_sample_data: dict) -> None:
        """The subway leg is correctly identified."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        subway_legs = [leg for leg in route.legs if leg.mode == "TRANSIT"]
        assert len(subway_legs) == 1

        subway = subway_legs[0]
        assert subway.mode == "TRANSIT"
        assert subway.system == "MTA Subway"
        assert subway.line == "C"

    def test_identifies_walking_legs(self, directions_sample_data: dict) -> None:
        """Walking legs are correctly identified."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        walk_legs = [leg for leg in route.legs if leg.mode == "WALKING"]
        assert len(walk_legs) == 2
        for walk in walk_legs:
            assert walk.mode == "WALKING"

    def test_depart_and_arrive_times(self, directions_sample_data: dict) -> None:
        """Departure and arrival times are correctly parsed."""
        route = _parse_route(directions_sample_data)

        assert route is not None
        assert route.depart_at.tzinfo is not None
        assert route.arrive_at.tzinfo is not None
        assert route.arrive_at > route.depart_at

    def test_raw_payload_stored(self, directions_sample_data: dict) -> None:
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
        response = {
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
        response = {
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
        response = {
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
        response = {
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
        for mode in ["transit", "driving", "walking", "bicycling"]:
            result = plan_route(origin, destination, arrival_time, mode=mode, api_key="")
            assert result is None  # No API key = None
