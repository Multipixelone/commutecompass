"""Test fixtures and shared pytest configuration."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from commutecompass.timeutil import NYC_TZ


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a Path for a temporary database file."""
    return tmp_path / "test.db"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def gtfs_rt_sample_path(fixtures_dir: Path) -> Path:
    """Return path to a sample GTFS-RT protobuf file."""
    return fixtures_dir / "gtfs_rt_sample.pb"


@pytest.fixture
def directions_sample_path(fixtures_dir: Path) -> Path:
    """Return path to a sample Google Directions JSON."""
    return fixtures_dir / "directions_sample.json"


@pytest.fixture
def calendar_sample_path(fixtures_dir: Path) -> Path:
    """Return path to a sample calendar event JSON."""
    return fixtures_dir / "calendar_sample.json"


@pytest.fixture
def scheduled_directions() -> dict[str, Any]:
    """Anonymous reproduction of the early-arriving route seen on October 3."""
    arrival = datetime(2026, 10, 3, 14, 40, 43, tzinfo=NYC_TZ)
    alternatives = []
    for duration, first_walk, last_walk in [(2811, 600, 600), (2691, 500, 500), (3095, 187, 168)]:
        departure = arrival - timedelta(seconds=duration)
        alternatives.append({"legs": [{
            "departure_time": {"value": int(departure.timestamp())},
            "arrival_time": {"value": int(arrival.timestamp())},
            "duration": {"value": duration},
            "steps": [
                {"travel_mode": "WALKING", "duration": {"value": first_walk}},
                {"travel_mode": "TRANSIT", "duration": {"value": 1500},
                 "transit_details": {
                     "line": {"short_name": "C", "vehicle": {"type": "SUBWAY"}},
                     "departure_stop": {"name": "Station A"},
                     "arrival_stop": {"name": "Station B"},
                 }},
                {"travel_mode": "WALKING", "duration": {"value": last_walk}},
            ],
        }]})
    return {"status": "OK", "routes": alternatives}
