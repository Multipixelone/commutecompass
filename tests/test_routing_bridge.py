"""Routing's optional enrichment is default-on and cannot break a route."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from bridge_helpers import BOARDING, NOW, build_index, directions

from commutecompass.gtfs_bridge import validate_leg
from commutecompass.gtfs_index import GtfsIndex
from commutecompass.models import Origin, ResolvedLocation
from commutecompass.routing import _parse_route, plan_route

OPEN_INDEX = GtfsIndex.open


def test_only_first_transit_leg_is_validated(tmp_path: Path) -> None:
    db = build_index(tmp_path / "gtfs.db")
    payload = directions()
    steps = payload["routes"][0]["legs"][0]["steps"]
    steps.append(deepcopy(steps[1]))
    with patch.object(GtfsIndex, "open", side_effect=lambda path: OPEN_INDEX(path, now=NOW)):
        route = _parse_route(payload, gtfs_db_path=str(db))
    assert route is not None
    assert route.legs[1].gtfs_provenance is not None
    assert route.legs[-1].gtfs_provenance is None
    assert route.raw_provider_payload == payload


@pytest.mark.parametrize("failure", ["missing", "stale", "corrupt", "exception", "mismatch"])
def test_bridge_fail_open(tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture) -> None:
    db = tmp_path / "gtfs.db"
    payload = directions()
    baseline = _parse_route(payload)
    if failure == "stale":
        build_index(db, now=NOW - timedelta(days=9))
    elif failure == "corrupt":
        db.write_bytes(b"not sqlite")
    elif failure in ("exception", "mismatch"):
        build_index(db)
    caplog.set_level("INFO", logger="commutecompass.routing")
    if failure in ("exception", "mismatch"):
        payload = deepcopy(payload)
        if failure == "mismatch":
            payload["routes"][0]["legs"][0]["steps"][1]["transit_details"]["num_stops"] = 99
            baseline = _parse_route(payload)
        with patch.object(GtfsIndex, "open", side_effect=lambda path: OPEN_INDEX(path, now=NOW)), patch(
            "commutecompass.routing.validate_leg",
            side_effect=RuntimeError("broken bridge") if failure == "exception" else None,
            wraps=validate_leg,
        ):
            actual = _parse_route(payload, gtfs_db_path=str(db))
    else:
        actual = _parse_route(payload, gtfs_db_path=str(db))
    assert actual is not None and baseline is not None
    # Walking timestamps are processing-time placeholders, not schedules.
    for actual_leg, baseline_leg in zip(actual.legs, baseline.legs, strict=True):
        if actual_leg.mode == "WALKING":
            baseline_leg.depart_at = actual_leg.depart_at
            baseline_leg.arrive_at = actual_leg.arrive_at
    assert actual == baseline
    assert "GTFS bridge" in caplog.text
    if failure == "exception":
        assert "WARNING" in caplog.text
    elif failure == "mismatch":
        assert "arrival_stop_missing" in caplog.text


def test_plan_route_passes_configured_path(tmp_path: Path) -> None:
    db = build_index(tmp_path / "gtfs.db")
    with patch("commutecompass.routing.httpx.Client") as client, patch.object(
        GtfsIndex, "open", side_effect=lambda path: OPEN_INDEX(path, now=NOW),
    ) as opened:
        client.return_value.__enter__.return_value.get.return_value.json.return_value = directions()
        route = plan_route(
            Origin(address="home", lat=40.679364, lon=-73.930729),
            ResolvedLocation(kind="address", value="work", source="geocode"),
            BOARDING + timedelta(minutes=20), api_key="test", gtfs_db_path=str(db),
        )
    opened.assert_called_once_with(str(db))
    assert route is not None and route.legs[1].gtfs_provenance is not None
