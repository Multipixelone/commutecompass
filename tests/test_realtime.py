"""Tests for realtime.py — GTFS-RT departure delay buffer.

Most cases inject a ``fetcher`` returning a canned predictions map (no network,
mirroring weather's fetcher injection).  Feed *parsing* is covered separately by
building ``FeedMessage`` protos in-memory (mirroring test_mta's hand-built
feeds), so no binary fixture is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from unittest.mock import patch

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs  # type: ignore[import-untyped]

from commutecompass.config import RealtimeConfig
from commutecompass.models import Route, TransitLeg
from commutecompass.realtime import (
    Predictions,
    RealtimeDelay,
    _accumulate,
    _fetch_predictions,
    _match_stop_ids,
    _normalize_feed_stop,
    _route_matches,
    realtime_delay,
)
from commutecompass.timeutil import NYC_TZ

SCHED = datetime(2026, 5, 8, 8, 0, 0, tzinfo=NYC_TZ)


def make_route(
    *,
    system: str | None = "MTA Subway",
    line: str | None = "Q",
    stop: str | None = "14 St-Union Sq",
    mode: str = "TRANSIT",
    depart: datetime = SCHED,
) -> Route:
    leg = TransitLeg(
        mode=mode,  # type: ignore[arg-type]
        system=system,
        line=line,
        headsign="Astoria",
        depart_at=depart,
        arrive_at=depart + timedelta(minutes=20),
        duration_seconds=1200,
        summary="leg",
        departure_stop=stop,
    )
    return Route(
        legs=[leg],
        depart_at=depart,
        arrive_at=depart + timedelta(minutes=20),
        total_duration_seconds=1200,
        transfers=0,
    )


def fetcher(preds: Predictions) -> Callable[[list[str], str], Predictions]:
    def _f(urls: list[str], system: str) -> Predictions:
        return preds

    return _f


# ── core delay computation ────────────────────────────────────────────────────


def test_delay_computed_and_surfaced() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=6))]}
    d = realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds))
    assert d == RealtimeDelay(6, "Q running ~6 min late")


def test_delay_capped_at_max_buffer() -> None:
    cfg = RealtimeConfig(enabled=True, max_buffer_minutes=10, match_window_minutes=30)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=20))]}
    d = realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds))
    assert d.minutes == 10


def test_delay_below_min_threshold_is_clear() -> None:
    cfg = RealtimeConfig(enabled=True, min_delay_minutes=2)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=1))]}
    assert realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds)).reason is None


def test_early_or_ontime_never_buffers() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("Q", SCHED - timedelta(minutes=5))]}  # train early
    assert realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds)).minutes == 0


def test_trip_outside_match_window_ignored() -> None:
    cfg = RealtimeConfig(enabled=True, match_window_minutes=10)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=45))]}  # not our trip
    assert realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds)).minutes == 0


def test_picks_trip_closest_to_scheduled() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {
        "R20N": [
            ("Q", SCHED + timedelta(minutes=4)),  # closest → this one
            ("Q", SCHED + timedelta(minutes=18)),
        ]
    }
    assert realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds)).minutes == 4


# ── stop / route / system matching ────────────────────────────────────────────


def test_fuzzy_stop_match_tolerates_name_variation() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=5))]}
    # Word-order/punctuation variation of "14 St-Union Sq".
    d = realtime_delay(make_route(stop="Union Sq - 14 St"), SCHED, cfg, fetcher=fetcher(preds))
    assert d.minutes == 5


def test_unmatchable_stop_name_is_clear() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=6))]}
    d = realtime_delay(make_route(stop="Zzzxxx Qqqwww"), SCHED, cfg, fetcher=fetcher(preds))
    assert d == RealtimeDelay(0, None)


def test_route_mismatch_is_clear() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("6", SCHED + timedelta(minutes=6))]}  # different line at same stop
    assert realtime_delay(make_route(line="Q"), SCHED, cfg, fetcher=fetcher(preds)).minutes == 0


def test_unknown_system_is_clear() -> None:
    cfg = RealtimeConfig(enabled=True)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=6))]}
    assert realtime_delay(make_route(system="Amtrak"), SCHED, cfg, fetcher=fetcher(preds)).minutes == 0


def test_no_transit_leg_is_clear() -> None:
    cfg = RealtimeConfig(enabled=True)
    route = make_route(mode="DRIVING", system=None, line=None, stop=None)
    assert realtime_delay(route, SCHED, cfg, fetcher=fetcher({})).minutes == 0


def test_lirr_matches_on_stop_ignoring_route_id() -> None:
    cfg = RealtimeConfig(enabled=True)
    # Atlantic Terminal LIRR stop_id is "241"; route_id is a branch number we ignore.
    preds = {"241": [("99", SCHED + timedelta(minutes=7))]}
    d = realtime_delay(
        make_route(system="LIRR", line="Babylon", stop="Atlantic Terminal"),
        SCHED,
        cfg,
        fetcher=fetcher(preds),
    )
    assert d.minutes == 7


def test_bus_route_id_prefix_stripped() -> None:
    cfg = RealtimeConfig(enabled=True)
    # A real bus stop name from the bundled table (stop_id 300000).
    preds = {"300000": [("MTA NYCT_M15", SCHED + timedelta(minutes=5))]}
    d = realtime_delay(
        make_route(system="MTA Bus", line="M15", stop="Oriental Blvd/Mackenzie St"),
        SCHED,
        cfg,
        fetcher=fetcher(preds),
    )
    assert d.minutes == 5


# ── fail-open / disabled ──────────────────────────────────────────────────────


def test_disabled_is_clear() -> None:
    cfg = RealtimeConfig(enabled=False)
    preds = {"R20N": [("Q", SCHED + timedelta(minutes=6))]}
    assert realtime_delay(make_route(), SCHED, cfg, fetcher=fetcher(preds)) == RealtimeDelay(0, None)


def test_fetcher_exception_fails_open() -> None:
    cfg = RealtimeConfig(enabled=True)

    def boom(urls: list[str], system: str) -> Predictions:
        raise RuntimeError("feed down")

    assert realtime_delay(make_route(), SCHED, cfg, fetcher=boom) == RealtimeDelay(0, None)


# ── unit helpers ──────────────────────────────────────────────────────────────


def test_match_stop_ids_expands_subway_directions() -> None:
    ids = _match_stop_ids("MTA Subway", "14 St-Union Sq", 80)
    # At least the Broadway (Q/N/R/W) complex R20 with directional children.
    assert "R20N" in ids and "R20S" in ids


@pytest.mark.parametrize(
    "system,feed_route,leg_line,expected",
    [
        ("MTA Subway", "Q", "Q", True),
        ("MTA Subway", "6X", "6", True),
        ("MTA Subway", "6", "Q", False),
        ("MTA Bus", "MTA NYCT_M15", "M15", True),
        ("MTA Bus", "MTABC_Q44", "Q44", True),
        ("MTA Bus", "MTA NYCT_M15", "M20", False),
        ("LIRR", "anything", "Babylon", True),
    ],
)
def test_route_matches(system: str, feed_route: str, leg_line: str, expected: bool) -> None:
    assert _route_matches(system, feed_route, leg_line) is expected


def test_normalize_feed_stop_strips_bus_prefix() -> None:
    assert _normalize_feed_stop("MTA_300000", "MTA Bus") == "300000"
    assert _normalize_feed_stop("R20N", "MTA Subway") == "R20N"


# ── feed parsing ──────────────────────────────────────────────────────────────


def _feed_with(
    stop_id: str, route_id: str, *, departure: datetime | None, arrival: datetime | None
) -> "gtfs.FeedMessage":
    feed = gtfs.FeedMessage()
    entity = feed.entity.add()
    entity.id = "e1"
    tu = entity.trip_update
    tu.trip.route_id = route_id
    stu = tu.stop_time_update.add()
    stu.stop_id = stop_id
    if departure is not None:
        stu.departure.time = int(departure.timestamp())
    if arrival is not None:
        stu.arrival.time = int(arrival.timestamp())
    return feed


def test_accumulate_prefers_departure_time() -> None:
    feed = _feed_with(
        "R20N", "Q", departure=SCHED + timedelta(minutes=6), arrival=SCHED + timedelta(minutes=5)
    )
    preds: Predictions = {}
    _accumulate(feed, preds, "MTA Subway")
    assert preds["R20N"] == [("Q", SCHED + timedelta(minutes=6))]


def test_accumulate_falls_back_to_arrival_time() -> None:
    feed = _feed_with("R20N", "Q", departure=None, arrival=SCHED + timedelta(minutes=3))
    preds: Predictions = {}
    _accumulate(feed, preds, "MTA Subway")
    assert preds["R20N"] == [("Q", SCHED + timedelta(minutes=3))]


def test_fetch_predictions_parses_via_shared_helper() -> None:
    feed = _feed_with("R20N", "Q", departure=SCHED + timedelta(minutes=6), arrival=None)
    with patch("commutecompass.realtime.fetch_feed_message", return_value=feed):
        preds = _fetch_predictions(["https://feed.example/subway"], "MTA Subway")
    assert "R20N" in preds


def test_default_path_uses_cached_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the non-injected path: realtime_delay -> _cached_fetch -> _fetch_predictions.
    import commutecompass.realtime as rt

    rt._feed_cache.clear()
    feed = _feed_with("R20N", "Q", departure=SCHED + timedelta(minutes=6), arrival=None)
    cfg = RealtimeConfig(enabled=True)
    with patch("commutecompass.realtime.fetch_feed_message", return_value=feed):
        d = realtime_delay(make_route(), SCHED, cfg)
    assert d == RealtimeDelay(6, "Q running ~6 min late")
    rt._feed_cache.clear()
