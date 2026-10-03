"""Offline serialized GTFS-RT regressions for fail-closed delay eligibility."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs  # type: ignore[import-untyped]

from commutecompass.config import RealtimeConfig
from commutecompass.models import Event, Plan, Route, TransitLeg
from commutecompass.realtime import (
    FetchResult, Predictions, _accumulate, _cached_fetch, _feed_cache,
    _fetch_predictions, _match_stop_ids, _route_matches, realtime_delay,
)
from commutecompass.timeutil import NYC_TZ
from commutecompass.store import Store

SCHED = datetime(2026, 5, 8, 8, tzinfo=NYC_TZ)
NOW = SCHED - timedelta(minutes=10)


def make_route(**overrides: Any) -> Route:
    fields: dict[str, Any] = dict(
        mode="TRANSIT", system="MTA Subway", line="Q", headsign="Astoria",
        depart_at=SCHED, arrive_at=SCHED + timedelta(minutes=20),
        duration_seconds=1200, summary="leg", departure_stop="14 St-Union Sq",
        scheduled_departure_valid=True, gtfs_trip_id="A", gtfs_route_id="Q",
        gtfs_start_date="20260508", gtfs_start_time="07:30:00",
        gtfs_boarding_stop_id="R20N", gtfs_boarding_stop_sequence=10, gtfs_direction_id=0,
    )
    fields.update(overrides)
    leg = TransitLeg(**fields)
    return Route(legs=[leg], depart_at=leg.depart_at, arrive_at=leg.arrive_at,
                 total_duration_seconds=1200)


def feed_with(
    *, delay: int | None = 360, departure: datetime | None = None,
    header: datetime | None = NOW, update: datetime | None = NOW,
    trip: str = "A", stop: str = "R20N", direction: int = 0,
    route: str = "Q", trip_relationship: int = 0, stop_relationship: int = 0,
    deleted: bool = False, differential: bool = False,
) -> Any:
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = int(differential)
    if header is not None:
        feed.header.timestamp = int(header.timestamp())
    entity = feed.entity.add()
    entity.id = trip
    entity.is_deleted = deleted
    tu = entity.trip_update
    if update is not None:
        tu.timestamp = int(update.timestamp())
    tu.trip.trip_id = trip
    tu.trip.route_id = route
    tu.trip.start_date = "20260508"
    tu.trip.start_time = "07:30:00"
    tu.trip.direction_id = direction
    tu.trip.schedule_relationship = trip_relationship
    stu = tu.stop_time_update.add()
    stu.stop_id = stop
    stu.stop_sequence = 10
    stu.schedule_relationship = stop_relationship
    if departure is None:
        departure = SCHED + timedelta(seconds=delay or 0)
    stu.departure.time = int(departure.timestamp())
    if delay is not None:
        stu.departure.delay = delay
    # Exercise presence bits and serialized protobufs, not reduced tuples.
    return gtfs.FeedMessage.FromString(feed.SerializeToString())


def result_for(feed: Any) -> FetchResult:
    predictions: Predictions = {}
    usable = _accumulate(feed, predictions, "MTA Subway", now=NOW)
    return FetchResult(predictions, usable_feeds=int(usable))


def observe(feed: Any, route: Route | None = None, **config: Any) -> Any:
    return realtime_delay(
        route or make_route(), SCHED + timedelta(days=1),
        RealtimeConfig(enabled=True, **config),
        fetcher=lambda urls, system: result_for(feed), clock=lambda: NOW,
    )


@pytest.mark.parametrize("age,expected", [(299, "unavailable"), (298, "observed")])
def test_fetch_revalidates_producer_age_with_live_clock(age: int, expected: str) -> None:
    current = NOW
    producer = NOW - timedelta(seconds=age)
    feed = feed_with(header=producer, update=producer)

    def fetch(*args: Any) -> Any:
        nonlocal current
        current += timedelta(seconds=2)
        return feed

    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.fetch_feed_message", side_effect=fetch,
    ), patch("commutecompass.realtime._cached_fetch", side_effect=lambda urls, system, fn: fn(urls, system)):
        result = realtime_delay(
            make_route(), SCHED,
            RealtimeConfig(enabled=True, subway_tripupdate_urls=["https://example.test/feed"]),
            clock=lambda: current,
        )
    assert result.status == expected
    assert result.minutes == (6 if expected == "observed" else 0)


def test_postfetch_rechecks_predictions_parsed_before_clock_advances() -> None:
    current = NOW
    producer = NOW - timedelta(seconds=299)
    parsed = result_for(feed_with(header=producer, update=producer))
    assert parsed.usable_feeds == 1

    def fetch(urls: list[str], system: str) -> FetchResult:
        nonlocal current
        current += timedelta(seconds=2)
        return parsed

    result = realtime_delay(make_route(), SCHED, RealtimeConfig(enabled=True),
                            fetcher=fetch, clock=lambda: current)
    assert result.status == "unavailable" and result.minutes == 0


def test_observed_delay_capped_and_thresholded() -> None:
    result = observe(feed_with())
    assert (result.minutes, result.reason, result.status) == (6, "Q running ~6 min late", "observed")
    assert observe(feed_with(delay=1200), max_buffer_minutes=10).minutes == 10
    assert observe(feed_with(delay=60), min_delay_minutes=2).minutes == 0
    assert observe(feed_with(delay=-300)).minutes == 0


def dst_observation(
    scheduled: datetime, seconds: int,
) -> tuple[Route, FetchResult, datetime, datetime]:
    """Serialized producer timestamps and an independent, fresh observation clock."""
    departure = datetime.fromtimestamp(scheduled.timestamp() + seconds, NYC_TZ)
    now = datetime.fromtimestamp(scheduled.timestamp() - 600, NYC_TZ)
    service_date = scheduled.strftime("%Y%m%d")
    feed = feed_with(delay=seconds, departure=departure, header=now, update=now)
    feed.entity[0].trip_update.trip.start_date = service_date
    feed = gtfs.FeedMessage.FromString(feed.SerializeToString())
    predictions: Predictions = {}
    usable = _accumulate(feed, predictions, "MTA Subway", now=now)
    route = make_route(
        depart_at=scheduled, gtfs_start_date=service_date,
        arrive_at=datetime.fromtimestamp(scheduled.timestamp() + 1200, NYC_TZ),
    )
    return route, FetchResult(predictions, usable_feeds=int(usable)), now, departure


@pytest.mark.parametrize("scheduled", [
    datetime(2026, 11, 1, 1, 55, tzinfo=NYC_TZ),
    datetime(2026, 3, 8, 1, 55, tzinfo=NYC_TZ),
])
@pytest.mark.parametrize("seconds,minutes", [
    (-600, 0), (0, 0), (60, 0), (600, 10), (1200, 10),
])
def test_dst_elapsed_delay_survives_json_and_sqlite(
    tmp_path: Path, scheduled: datetime, seconds: int, minutes: int,
) -> None:
    route, feed_result, now, departure = dst_observation(scheduled, seconds)
    assert scheduled.tzinfo is NYC_TZ and departure.tzinfo is NYC_TZ
    assert departure.timestamp() - scheduled.timestamp() == seconds
    if seconds == 600:
        expected = "2026-11-01T01:05:00-05:00" if scheduled.month == 11 else "2026-03-08T03:05:00-04:00"
        assert departure.isoformat() == expected

    config = RealtimeConfig(enabled=True, min_delay_minutes=2, max_buffer_minutes=10)

    def measure(candidate: Route) -> Any:
        return realtime_delay(
            candidate, SCHED, config, fetcher=lambda urls, system: feed_result,
            clock=lambda: now,
        )

    expected_delay = measure(route)
    assert expected_delay.status == "observed" and expected_delay.minutes == minutes
    assert expected_delay.departures == (departure,)
    assert measure(Route.model_validate_json(route.model_dump_json())) == expected_delay

    store = Store(tmp_path / "dst.sqlite")
    store.init_schema()
    event = Event(id="dst", calendar_id="cal", calendar_name="Test", title="DST",
                  start=route.arrive_at, end=route.arrive_at)
    store.upsert_plan(Plan(event=event, route=route))
    restored = store.get_plan(event.id)
    assert restored is not None and restored.route is not None
    assert restored.route.legs[0].depart_at.timestamp() == scheduled.timestamp()
    assert measure(restored.route) == expected_delay


@pytest.mark.parametrize("fold", [0, 1])
@pytest.mark.parametrize("fixed_offset", [False, True])
def test_identical_fold_wall_times_are_not_same_boarding_instant(
    fold: int, fixed_offset: bool,
) -> None:
    scheduled = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=fold)
    route, result, now, _ = dst_observation(scheduled, 0)
    wrong = scheduled.replace(fold=1 - fold)
    if fixed_offset:
        wrong = datetime.fromisoformat(wrong.isoformat())
    route.legs[0].depart_at = wrong
    delay = realtime_delay(route, SCHED, RealtimeConfig(enabled=True),
                           fetcher=lambda urls, system: result, clock=lambda: now)
    assert delay.status == "unmatched" and delay.minutes == 0


def test_informational_departures_sorted_and_deduplicated_by_instant() -> None:
    scheduled = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ)
    route, result, now, first = dst_observation(scheduled, 0)
    second = datetime.fromtimestamp(first.timestamp() + 3600, NYC_TZ)
    earlier_wall = datetime.fromtimestamp(first.timestamp() + 2400, NYC_TZ)
    feed = feed_with(trip="B", departure=second, header=now, update=now)
    feed.entity.add().CopyFrom(feed_with(
        trip="C", departure=earlier_wall, header=now, update=now,
    ).entity[0])
    _accumulate(feed, result.predictions, "MTA Subway", now=now)
    # Duplicate the first instant with a fixed-offset representation.
    original = result.predictions["R20N"][0]
    result.predictions["R20N"].append(replace(
        original, trip_id="D", departure=datetime.fromisoformat(first.isoformat()),
    ))
    delay = realtime_delay(route, SCHED, RealtimeConfig(enabled=True),
                           fetcher=lambda urls, system: result, clock=lambda: now)
    assert delay.status == "observed"
    assert [value.timestamp() for value in delay.departures] == [
        first.timestamp(), earlier_wall.timestamp(), second.timestamp(),
    ]
    assert all(value.tzinfo is NYC_TZ for value in delay.departures)


def test_freshness_across_fold_uses_elapsed_seconds() -> None:
    scheduled = datetime(2026, 11, 1, 1, 5, tzinfo=NYC_TZ, fold=1)
    route, result, now, _ = dst_observation(scheduled, 600)
    assert now.isoformat() == "2026-11-01T01:55:00-04:00"
    for age, status in [(300, "observed"), (301, "unavailable")]:
        observed_at = datetime.fromtimestamp(now.timestamp() + age, NYC_TZ)
        delay = realtime_delay(route, SCHED, RealtimeConfig(enabled=True),
                               fetcher=lambda urls, system: result, clock=lambda: observed_at)
        assert delay.status == status


def test_unrelated_nearer_ontime_train_is_never_measured_as_delay() -> None:
    feed = feed_with(delay=1200)
    other = feed_with(trip="B", delay=0, departure=SCHED + timedelta(minutes=4))
    feed.entity.add().CopyFrom(other.entity[0])
    delay = observe(feed, max_buffer_minutes=30)
    assert delay.minutes == 20
    assert delay.status == "observed"
    assert observe(other).status == "unmatched"


def test_missing_delay_distinct_from_explicit_zero() -> None:
    missing = result_for(feed_with(delay=None)).predictions["R20N"][0]
    zero = result_for(feed_with(delay=0)).predictions["R20N"][0]
    assert missing.delay is None
    assert zero.delay == 0
    assert observe(feed_with(delay=None)).status == "unmatched"
    assert observe(feed_with(delay=0)).status == "observed"


def test_delay_only_preserved_but_not_measurable() -> None:
    feed = feed_with()
    feed.entity[0].trip_update.stop_time_update[0].departure.ClearField("time")
    prediction = result_for(feed).predictions["R20N"][0]
    assert prediction.delay == 360 and prediction.departure is None
    assert prediction.trip_id == "A" and prediction.stop_sequence == 10
    assert observe(feed).status == "unmatched"


def test_arrival_only_is_information_not_boarding_delay() -> None:
    feed = feed_with()
    stu = feed.entity[0].trip_update.stop_time_update[0]
    stu.ClearField("departure")
    stu.arrival.time = int((SCHED + timedelta(minutes=5)).timestamp())
    stu.arrival.delay = 300
    prediction = result_for(feed).predictions["R20N"][0]
    assert prediction.arrival is not None and prediction.departure is None
    assert prediction.arrival_delay == 300
    assert observe(feed).status == "unmatched"


@pytest.mark.parametrize("fields", [
    {"gtfs_trip_id": None}, {"gtfs_route_id": None}, {"gtfs_start_date": None},
    {"gtfs_direction_id": None}, {"gtfs_boarding_stop_sequence": None},
    {"gtfs_start_date": "20260509"}, {"gtfs_start_time": "08:30:00"},
    {"gtfs_boarding_stop_sequence": 11}, {"gtfs_route_id": "R"},
    {"gtfs_direction_id": 1}, {"gtfs_boarding_stop_id": "R20S"},
    {"depart_at": SCHED + timedelta(minutes=1)},
])
def test_missing_or_different_independent_context_is_unknown(fields: dict[str, Any]) -> None:
    result = observe(feed_with(), make_route(**fields))
    assert result.minutes == 0 and result.status == "unmatched"


def test_directions_style_route_keeps_information_without_precision() -> None:
    route = make_route(gtfs_trip_id=None)
    result = observe(feed_with(), route)
    assert result.status == "unmatched" and result.minutes == 0
    assert result.departures == (SCHED + timedelta(minutes=6),)


@pytest.mark.parametrize("field", ["trip_id", "route_id", "start_date", "start_time", "direction_id"])
def test_missing_producer_trip_identity_never_matches(field: str) -> None:
    feed = feed_with()
    feed.entity[0].trip_update.trip.ClearField(field)
    result = observe(feed)
    assert result.status == "unmatched" and result.minutes == 0


def test_missing_producer_stop_sequence_never_matches() -> None:
    feed = feed_with()
    feed.entity[0].trip_update.stop_time_update[0].ClearField("stop_sequence")
    assert observe(feed).status == "unmatched"


def test_opposite_direction_closer_prediction_rejected() -> None:
    feed = feed_with(delay=1200)
    opposite = feed_with(delay=0, departure=SCHED + timedelta(minutes=1), stop="R20S", direction=1)
    feed.entity.add().CopyFrom(opposite.entity[0])
    assert observe(feed, max_buffer_minutes=30).minutes == 20
    assert observe(opposite).status == "unmatched"


def test_same_name_stations_are_not_pooled() -> None:
    # G20 (Queens) and R36 (Brooklyn) are geographically distinct "36 St".
    assert _match_stop_ids("MTA Subway", "36 St", 80) == []
    assert observe(feed_with(stop="G20N"), make_route(
        departure_stop="36 St", gtfs_boarding_stop_id=None,
    )).status == "unmatched"
    assert observe(feed_with(stop="G20N"), make_route(gtfs_boarding_stop_id="R36N")).status == "unmatched"


def test_lirr_requires_explicit_branch_and_identity() -> None:
    assert not _route_matches("LIRR", "99", "Babylon")
    route = make_route(system="LIRR", line="Babylon", departure_stop="Atlantic Terminal",
                       gtfs_boarding_stop_id="241", gtfs_route_id="1")
    assert observe(feed_with(stop="241", route="99"), route).status == "unmatched"
    assert observe(feed_with(stop="241", route="1"), route).status == "observed"
    assert observe(feed_with(stop="241", route="1"), route.model_copy(
        update={"legs": [route.legs[0].model_copy(update={"gtfs_route_id": None})]}
    )).status == "unmatched"


@pytest.mark.parametrize("header,update", [
    (NOW - timedelta(minutes=6), NOW),
    (NOW, NOW - timedelta(minutes=6)),
    (NOW + timedelta(seconds=61), NOW),
    (NOW, NOW + timedelta(seconds=61)),
    (None, NOW), (None, None),
])
def test_stale_skewed_or_missing_observations_are_ineligible(
    header: datetime | None, update: datetime | None,
) -> None:
    feed = feed_with(header=header, update=update)
    prediction = result_for(feed).predictions["R20N"][0]
    assert not prediction.fresh
    result = observe(feed)
    assert result.minutes == 0 and result.status in {"unavailable", "unmatched"}


def test_missing_tu_timestamp_uses_fresh_header() -> None:
    assert observe(feed_with(update=None)).status == "observed"


def test_cached_observation_rechecked_against_clock_not_planning_time() -> None:
    result = result_for(feed_with())
    delay = realtime_delay(make_route(), SCHED + timedelta(days=3), RealtimeConfig(enabled=True),
                           fetcher=lambda urls, system: result,
                           clock=lambda: NOW + timedelta(minutes=6))
    assert delay.status == "unavailable" and delay.minutes == 0


@pytest.mark.parametrize("trip_relationship,stop_relationship,status", [
    (3, 0, "cancelled"), (0, 1, "skipped"), (0, 2, "unmatched"),
    (1, 0, "unmatched"), (2, 0, "unmatched"),
])
def test_schedule_relationships(trip_relationship: int, stop_relationship: int, status: str) -> None:
    result = observe(feed_with(trip_relationship=trip_relationship, stop_relationship=stop_relationship))
    assert result.status == status and result.minutes == 0
    assert not result.departures


def test_trip_cancellation_without_stop_updates() -> None:
    feed = feed_with(trip_relationship=3)
    feed.entity[0].trip_update.ClearField("stop_time_update")
    assert observe(feed).status == "cancelled"


@pytest.mark.parametrize("deleted,differential", [(True, False), (False, True)])
def test_deleted_and_differential_updates_not_applied(deleted: bool, differential: bool) -> None:
    result = observe(feed_with(deleted=deleted, differential=differential))
    assert result.minutes == 0 and result.status != "observed"


def test_duplicate_boarding_updates_are_ambiguous() -> None:
    feed = feed_with()
    feed.entity.add().CopyFrom(feed.entity[0])
    assert observe(feed).status == "unmatched"


def test_walk_unknown_system_and_no_coverage() -> None:
    assert observe(feed_with(), make_route(mode="WALKING")).status == "not_applicable"
    assert observe(feed_with(), make_route(system="Amtrak")).status == "not_applicable"
    assert observe(feed_with(), subway_tripupdate_urls=[]).status == "not_applicable"


def test_disabled_and_untrusted_schedules_do_not_fetch() -> None:
    def boom(urls: list[str], system: str) -> FetchResult:
        pytest.fail("must not fetch")
    assert realtime_delay(make_route(), SCHED, RealtimeConfig(), fetcher=boom).minutes == 0
    delay = realtime_delay(make_route(scheduled_departure_valid=False), SCHED,
                           RealtimeConfig(enabled=True), fetcher=boom)
    assert delay.status == "unmatched" and delay.minutes == 0


def test_fetch_exception_fails_open_but_reports_failure() -> None:
    def boom(urls: list[str], system: str) -> FetchResult:
        raise RuntimeError("timeout")
    result = realtime_delay(make_route(), SCHED, RealtimeConfig(enabled=True), fetcher=boom)
    assert result.minutes == 0 and result.status == "unavailable" and result.feed_failed


def test_fetch_partial_success_and_failure_retained() -> None:
    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.retry", side_effect=[RuntimeError("timeout"), feed_with()],
    ):
        result = _fetch_predictions(["https://bad", "https://good"], "MTA Subway", clock=lambda: NOW)
    assert result.failures == 1 and result.usable_feeds == 1
    assert result.predictions["R20N"][0].trip_id == "A"
    delay = realtime_delay(make_route(), SCHED, RealtimeConfig(enabled=True),
                           fetcher=lambda urls, system: result, clock=lambda: NOW)
    assert delay.status == "observed" and delay.minutes == 6 and delay.feed_failed


def test_fetch_budget_retains_partial_observation_and_skips_later_feeds() -> None:
    ticks = [0.0]
    timeouts: list[float | None] = []
    urls: list[str] = []

    def fetch(url: str, system: str, client: Any) -> Any:
        urls.append(url)
        timeouts.append(client.timeout.read)
        if url == "good":
            ticks[0] = 4.0
            return feed_with()
        ticks[0] = 5.0
        import httpx
        raise httpx.ReadTimeout("deadline reached")

    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.fetch_feed_message", side_effect=fetch,
    ), patch("commutecompass.retry.time.sleep") as sleep:
        result = _fetch_predictions(["good", "slow", "skipped"], "MTA Subway",
                                    clock=lambda: NOW, monotonic=lambda: ticks[0])
    assert urls == ["good", "slow"]  # Neither retry I/O nor later feeds start.
    assert timeouts == [0.5, 0.25]
    assert all(call.args == (0.0,) for call in sleep.call_args_list)
    assert result.usable_feeds == 1 and result.failures == 2
    delay = realtime_delay(make_route(), SCHED, RealtimeConfig(enabled=True),
                           fetcher=lambda urls, system: result, clock=lambda: NOW)
    assert delay.status == "observed" and delay.minutes == 6 and delay.feed_failed


def test_fetch_retry_timeout_uses_remaining_budget() -> None:
    import httpx

    ticks = [0.0]
    timeouts: list[float | None] = []

    def fetch(url: str, system: str, client: Any) -> Any:
        timeouts.append(client.timeout.read)
        if len(timeouts) == 1:
            ticks[0] = 4.5
            raise httpx.ReadTimeout("retryable")
        return feed_with()

    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.fetch_feed_message", side_effect=fetch,
    ), patch("commutecompass.retry.time.sleep"):
        result = _fetch_predictions(["good"], "MTA Subway", clock=lambda: NOW,
                                    monotonic=lambda: ticks[0])
    assert timeouts == [0.5, 0.125]
    assert result.usable_feeds == 1 and result.failures == 0


def test_fetch_parses_serialized_feed_via_shared_helper() -> None:
    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.fetch_feed_message", return_value=feed_with(),
    ) as shared_fetch:
        result = _fetch_predictions(["https://good"], "MTA Subway", clock=lambda: NOW)
    shared_fetch.assert_called_once()
    prediction = result.predictions["R20N"][0]
    assert result.usable_feeds == 1 and prediction.delay == 360
    assert prediction.header_timestamp == int(NOW.timestamp())
    assert prediction.update_timestamp == int(NOW.timestamp())


def test_partial_parse_failure_cannot_leave_eligible_predictions() -> None:
    broken = feed_with()
    bad = broken.entity.add()
    bad.CopyFrom(broken.entity[0])
    bad.id = "broken"
    bad.trip_update.stop_time_update[0].departure.time = 2**63 - 1
    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.retry", side_effect=[broken, feed_with(trip="B")],
    ):
        result = _fetch_predictions(["https://broken", "https://good"], "MTA Subway", clock=lambda: NOW)
    assert result.failures == 1 and result.usable_feeds == 1
    assert [pred.trip_id for pred in result.predictions["R20N"]] == ["B"]


def test_all_feed_failures_not_empty_success() -> None:
    with patch("commutecompass.realtime.httpx.Client"), patch(
        "commutecompass.realtime.retry", side_effect=RuntimeError("timeout"),
    ):
        result = _fetch_predictions(["https://bad"], "MTA Subway", clock=lambda: NOW)
    assert result.failures == 1 and result.usable_feeds == 0


def test_cache_insertion_after_fetch_system_key_and_expiry() -> None:
    _feed_cache.clear()
    clock = [100.0]
    calls: list[str] = []
    result = FetchResult(failures=1)

    def fetch(urls: list[str], system: str) -> FetchResult:
        calls.append(system)
        clock[0] += 20
        return result

    def cached(system: str) -> FetchResult:
        return _cached_fetch(["https://same"], system, fetch, monotonic=lambda: clock[0])

    try:
        assert cached("MTA Subway") is result
        clock[0] = 179
        assert cached("MTA Subway") is result  # insertion=120, not 100
        assert calls == ["MTA Subway"]
        assert cached("LIRR") is result
        assert calls == ["MTA Subway", "LIRR"]
        clock[0] = 180
        assert cached("MTA Subway").failures == 1
        assert len(calls) == 3
    finally:
        _feed_cache.clear()
