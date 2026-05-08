"""Test fixtures and shared pytest configuration."""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest


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