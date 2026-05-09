"""Tests for store.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from commutecompass.store import Store
from commutecompass.models import (
    Alert,
    Event,
    Plan,
    PingEntry,
    ResolvedLocation,
    Route,
    TransitLeg,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_event(
    event_id: str = "evt-001",
    start_offset_hours: int = 3,
    end_offset_hours: int = 5,
) -> Event:
    """Make a test event with start time offset from now."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=start_offset_hours)
    end = now + timedelta(hours=end_offset_hours)
    return Event(
        id=event_id,
        calendar_id="cal-001",
        calendar_name="Test Calendar",
        title="Test Event",
        start=start,
        end=end,
        location_raw="200 Example St, New York, NY 10001",
        location_resolved=ResolvedLocation(
            kind="address",
            value="200 Example St, New York, NY 10001",
            lat=40.7128,
            lon=-74.0060,
            source="geocode",
        ),
    )


def make_route(start: datetime) -> Route:
    """Make a test route with one transit leg."""
    return Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St",
                depart_at=start - timedelta(minutes=45),
                arrive_at=start - timedelta(minutes=5),
                duration_seconds=2400,
                summary="C train from Jay St-MetroTech to Fulton St",
            ),
        ],
        depart_at=start - timedelta(minutes=45),
        arrive_at=start - timedelta(minutes=5),
        total_duration_seconds=2400,
        transfers=0,
    )


def make_plan(event: Event, with_route: bool = True) -> Plan:
    """Make a test plan."""
    return Plan(
        event=event,
        route=make_route(event.start) if with_route else None,
        leave_at=event.start - timedelta(minutes=65),
        prep_at=event.start - timedelta(minutes=85),
        error=None,
    )


def make_ping(event_id: str = "evt-001", fire_offset_minutes: int = -10) -> PingEntry:
    """Make a test ping entry."""
    return PingEntry(
        id="ping-001",
        event_id=event_id,
        kind="prep",
        fire_at=datetime.now(timezone.utc) + timedelta(minutes=fire_offset_minutes),
        fired=False,
        fired_at=None,
        message="Start getting ready",
    )


# ── Schema init tests ──────────────────────────────────────────────────────────

def test_store_init(tmp_db_path) -> None:
    """Store can be instantiated."""
    store = Store(tmp_db_path)
    assert store.db_path == tmp_db_path


def test_store_init_schema_creates_tables(tmp_db_path) -> None:
    """init_schema creates all four tables without raising."""
    store = Store(tmp_db_path)
    store.init_schema()
    # Verify tables exist by executing a query on each
    with __import__("sqlite3").connect(tmp_db_path) as conn:
        for table in ["plans", "pings", "geocode_cache", "alerts_seen"]:
            result = conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            ).fetchone()
            assert result is not None, f"Table {table} not created"


def test_store_init_schema_idempotent(tmp_db_path) -> None:
    """Calling init_schema twice does not raise."""
    store = Store(tmp_db_path)
    store.init_schema()
    store.init_schema()  # should not raise


# ── Plan CRUD tests ────────────────────────────────────────────────────────────

def test_upsert_plan_insert(tmp_db_path) -> None:
    """upsert_plan inserts a new plan."""
    store = Store(tmp_db_path)
    store.init_schema()
    event = make_event("evt-upsert-1")
    plan = make_plan(event)
    store.upsert_plan(plan)

    retrieved = store.get_plan("evt-upsert-1")
    assert retrieved is not None
    assert retrieved.event.id == "evt-upsert-1"
    assert retrieved.event.title == "Test Event"
    assert retrieved.route is not None
    assert retrieved.leave_at is not None
    assert retrieved.prep_at is not None


def test_upsert_plan_replace(tmp_db_path) -> None:
    """upsert_plan replaces an existing plan for the same event_id."""
    store = Store(tmp_db_path)
    store.init_schema()
    event1 = make_event("evt-replace", start_offset_hours=3)
    plan1 = make_plan(event1)
    store.upsert_plan(plan1)

    event2 = make_event("evt-replace", start_offset_hours=4)
    plan2 = make_plan(event2)
    store.upsert_plan(plan2)

    retrieved = store.get_plan("evt-replace")
    assert retrieved is not None
    assert retrieved.event.start.isoformat() == event2.start.isoformat()


def test_get_plan_missing(tmp_db_path) -> None:
    """get_plan returns None for non-existent event."""
    store = Store(tmp_db_path)
    store.init_schema()
    assert store.get_plan("nonexistent") is None


def test_today_plans_returns_today(tmp_db_path) -> None:
    """today_plans returns plans with event_start today in NYC."""
    store = Store(tmp_db_path)
    store.init_schema()

    # Create event starting in 2 hours (today)
    event = make_event("evt-today", start_offset_hours=2, end_offset_hours=4)
    plan = make_plan(event)
    store.upsert_plan(plan)

    plans = store.today_plans()
    assert len(plans) >= 1
    assert any(p.event.id == "evt-today" for p in plans)


def test_today_plans_excludes_tomorrow(tmp_db_path) -> None:
    """today_plans excludes events starting tomorrow."""
    store = Store(tmp_db_path)
    store.init_schema()

    from commutecompass.timeutil import NYC_TZ

    # Create event starting tomorrow
    tomorrow = datetime.now(NYC_TZ) + timedelta(days=1)
    tomorrow = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    event = Event(
        id="evt-tomorrow",
        calendar_id="cal-001",
        calendar_name="Test",
        title="Tomorrow Event",
        start=tomorrow,
        end=tomorrow + timedelta(hours=2),
    )
    plan = Plan(event=event, route=None, leave_at=None, prep_at=None, error=None)
    store.upsert_plan(plan)

    plans = store.today_plans()
    assert not any(p.event.id == "evt-tomorrow" for p in plans)


def test_today_plans_before_2am_includes_0130_as_previous_day(tmp_db_path) -> None:
    """When now is before 2AM NYC, an event at 01:30 NYC belongs to the previous logical day."""
    from commutecompass.timeutil import NYC_TZ, logical_day_bounds_nyc

    # Fixed reference: Saturday 2026-05-09 01:30 NYC — logical day is Fri May 8 02:00
    ref = datetime(2026, 5, 9, 1, 30, tzinfo=NYC_TZ)
    with patch("commutecompass.timeutil.now_nyc", return_value=ref):
        day_start, day_end = logical_day_bounds_nyc(ref)
        # Verify our assumption: event at 01:30 today is BEFORE day_start → previous day
        event_at_0130 = Event(
            id="evt-0130",
            calendar_id="cal-001",
            calendar_name="Test",
            title="Late Night Rehearsal",
            start=ref,
            end=ref + timedelta(hours=2),
        )
        plan = Plan(event=event_at_0130, route=None, leave_at=None, prep_at=None)

        store = Store(tmp_db_path)
        store.init_schema()
        store.upsert_plan(plan)

        # today_plans() uses logical_day_bounds_nyc() internally — patch there
        with patch("commutecompass.timeutil.now_nyc", return_value=ref):
            plans = store.today_plans()

        # The 01:30 event must appear because it falls in the previous logical day
        assert any(p.event.id == "evt-0130" for p in plans)


def test_today_plans_after_2am_includes_0230_as_current_day(tmp_db_path) -> None:
    """When now is after 2AM NYC, an event at 02:30 NYC belongs to the current logical day."""
    from commutecompass.timeutil import NYC_TZ, logical_day_bounds_nyc

    # Fixed reference: Saturday 2026-05-09 03:00 NYC — logical day is Sat May 9 02:00
    ref = datetime(2026, 5, 9, 3, 0, tzinfo=NYC_TZ)
    with patch("commutecompass.timeutil.now_nyc", return_value=ref):
        day_start, day_end = logical_day_bounds_nyc(ref)
        # Verify our assumption: event at 02:30 today is AFTER day_start → current day
        event_at_0230 = Event(
            id="evt-0230",
            calendar_id="cal-001",
            calendar_name="Test",
            title="Early Morning Call",
            start=ref.replace(hour=2, minute=30),
            end=ref.replace(hour=4, minute=30),
        )
        plan = Plan(event=event_at_0230, route=None, leave_at=None, prep_at=None)

        store = Store(tmp_db_path)
        store.init_schema()
        store.upsert_plan(plan)

        with patch("commutecompass.timeutil.now_nyc", return_value=ref):
            plans = store.today_plans()

        # The 02:30 event must appear because it falls in the current logical day
        assert any(p.event.id == "evt-0230" for p in plans)


def test_delete_old_plans(tmp_db_path) -> None:
    """delete_old_plans removes plans with event_start before the given datetime."""
    store = Store(tmp_db_path)
    store.init_schema()

    # Old event (yesterday)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    old_event = Event(
        id="evt-old",
        calendar_id="cal-001",
        calendar_name="Test",
        title="Old Event",
        start=yesterday,
        end=yesterday + timedelta(hours=1),
    )
    old_plan = Plan(event=old_event, route=None, leave_at=None, prep_at=None)
    store.upsert_plan(old_plan)

    # New event (today)
    new_event = make_event("evt-new", start_offset_hours=3)
    new_plan = make_plan(new_event)
    store.upsert_plan(new_plan)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    deleted = store.delete_old_plans(cutoff)
    assert deleted >= 1
    assert store.get_plan("evt-old") is None
    assert store.get_plan("evt-new") is not None


# ── Ping CRUD tests ────────────────────────────────────────────────────────────

def test_schedule_ping_insert(tmp_db_path) -> None:
    """schedule_ping inserts a ping entry."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-ping-1", fire_offset_minutes=30)
    store.schedule_ping(ping)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(pending) >= 1
    assert any(p.id == "ping-001" for p in pending)


def test_cancel_pings(tmp_db_path) -> None:
    """cancel_pings removes all pings for an event."""
    store = Store(tmp_db_path)
    store.init_schema()

    ping1 = PingEntry(
        id="ping-cancel-1",
        event_id="evt-cancel",
        kind="prep",
        fire_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        fired=False,
        message="Prep",
    )
    ping2 = PingEntry(
        id="ping-cancel-2",
        event_id="evt-cancel",
        kind="leave",
        fire_at=datetime.now(timezone.utc) + timedelta(minutes=50),
        fired=False,
        message="Leave",
    )
    store.schedule_ping(ping1)
    store.schedule_ping(ping2)

    cancelled = store.cancel_pings("evt-cancel")
    assert cancelled == 2
    assert store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=2)) == []


def test_pending_pings_filters_fired(tmp_db_path) -> None:
    """pending_pings excludes already-fired pings."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-pending", fire_offset_minutes=-5)
    store.schedule_ping(ping)

    # Should appear before marking fired
    before_mark = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert any(p.id == "ping-001" for p in before_mark)

    # Mark as fired
    store.mark_fired("ping-001", datetime.now(timezone.utc))

    # Should not appear after marking fired
    after_mark = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert not any(p.id == "ping-001" for p in after_mark)


def test_mark_fired(tmp_db_path) -> None:
    """mark_fired sets fired=1 and fired_at timestamp."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-fired", fire_offset_minutes=15)
    store.schedule_ping(ping)

    fired_at = datetime.now(timezone.utc)
    store.mark_fired("ping-001", fired_at)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert not any(p.id == "ping-001" for p in pending)


def test_pending_pings_respects_before_bound(tmp_db_path) -> None:
    """pending_pings only returns pings with fire_at <= before."""
    store = Store(tmp_db_path)
    store.init_schema()

    future_ping = PingEntry(
        id="ping-future",
        event_id="evt-future",
        kind="prep",
        fire_at=datetime.now(timezone.utc) + timedelta(hours=5),
        fired=False,
        message="Future",
    )
    store.schedule_ping(future_ping)

    # Query with a before time 1 hour from now — the 5-hour-ahead ping should not appear
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    pending = store.pending_pings(soon)
    assert not any(p.id == "ping-future" for p in pending)


# ── Geocode cache tests ────────────────────────────────────────────────────────

def test_cache_geocode_insert_and_retrieve(tmp_db_path) -> None:
    """cache_geocode and get_geocode round-trip a ResolvedLocation."""
    store = Store(tmp_db_path)
    store.init_schema()
    resolved = ResolvedLocation(
        kind="address",
        value="200 Example St, New York, NY 10001",
        lat=40.7128,
        lon=-74.0060,
        source="geocode",
    )
    store.cache_geocode("200 Example St", resolved)

    cached = store.get_geocode("200 Example St")
    assert cached is not None
    assert cached.kind == "address"
    assert cached.value == "200 Example St, New York, NY 10001"
    assert cached.lat == 40.7128


def test_get_geocode_expired(tmp_db_path) -> None:
    """get_geocode returns None for stale cache entries."""
    store = Store(tmp_db_path)
    store.init_schema()
    resolved = ResolvedLocation(
        kind="station",
        value="Example LIRR Station, NY",
        lat=40.6620,
        lon=-73.6310,
        source="llm",
    )
    store.cache_geocode("Example Centre", resolved)

    # Request with max_age_days=0 should treat any cached entry as expired
    cached = store.get_geocode("Example Centre", max_age_days=0)
    assert cached is None


def test_get_geocode_miss(tmp_db_path) -> None:
    """get_geocode returns None for uncached raw strings."""
    store = Store(tmp_db_path)
    store.init_schema()
    assert store.get_geocode("never-cached-address") is None


# ── Alert ledger tests ─────────────────────────────────────────────────────────

def test_mark_alert_seen_and_is_alert_seen(tmp_db_path) -> None:
    """mark_alert_seen and is_alert_seen round-trip."""
    store = Store(tmp_db_path)
    store.init_schema()

    store.mark_alert_seen("alert-abc", "evt-001")
    assert store.is_alert_seen("alert-abc", "evt-001") is True
    assert store.is_alert_seen("alert-abc", "evt-002") is False
    assert store.is_alert_seen("alert-xyz", "evt-001") is False


def test_mark_alert_seen_idempotent(tmp_db_path) -> None:
    """mark_alert_seen is idempotent — calling twice does not raise."""
    store = Store(tmp_db_path)
    store.init_schema()
    store.mark_alert_seen("alert-dup", "evt-dup")
    store.mark_alert_seen("alert-dup", "evt-dup")  # should not raise


# ── Round-trip integrity tests ─────────────────────────────────────────────────

def test_plan_round_trip_with_datetime_iso8601_offset(tmp_db_path) -> None:
    """Plan JSON round-trips with timezone-aware datetimes preserved."""
    store = Store(tmp_db_path)
    store.init_schema()
    event = make_event("evt-roundtrip", start_offset_hours=3)
    plan = make_plan(event)
    store.upsert_plan(plan)

    retrieved = store.get_plan("evt-roundtrip")
    assert retrieved is not None

    # Verify datetime fields preserve offset
    assert retrieved.event.start.tzinfo is not None
    assert retrieved.leave_at is not None
    assert retrieved.leave_at.tzinfo is not None

    # Parse from stored JSON directly and verify isoformat matches
    with __import__("sqlite3").connect(tmp_db_path) as conn:
        row = conn.execute(
            "SELECT plan_json FROM plans WHERE event_id = ?", ("evt-roundtrip",)
        ).fetchone()
    stored_data = json.loads(row[0])
    # Stored datetime strings should be ISO-8601 with offset
    assert "+" in stored_data["event"]["start"] or "Z" in stored_data["event"]["start"]


def test_ping_entry_round_trip_with_datetime(tmp_db_path) -> None:
    """PingEntry round-trips with fired_at preserved as ISO-8601."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = PingEntry(
        id="ping-rt",
        event_id="evt-rt",
        kind="leave",
        fire_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        fired=True,
        fired_at=datetime.now(timezone.utc),
        message="Leave now",
    )
    store.schedule_ping(ping)
    store.mark_fired("ping-rt", ping.fired_at)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    fired_ping = next((p for p in pending if p.id == "ping-rt"), None)
    # After marking fired it won't appear in pending; verify via direct query
    with __import__("sqlite3").connect(tmp_db_path) as conn:
        row = conn.execute(
            "SELECT fired_at FROM pings WHERE id = ?", ("ping-rt",)
        ).fetchone()
    assert row[0] is not None
    # Verify it's a valid ISO-8601 string with offset
    assert "+" in row[0] or "Z" in row[0]
    datetime.fromisoformat(row[0])  # should not raise


def test_resolved_location_round_trip(tmp_db_path) -> None:
    """ResolvedLocation JSON round-trips with all fields including None fields."""
    store = Store(tmp_db_path)
    store.init_schema()
    resolved = ResolvedLocation(
        kind="station",
        value="Example LIRR Station, NY",
        lat=None,
        lon=None,
        source="llm",
    )
    store.cache_geocode("Example Centre", resolved)

    cached = store.get_geocode("Example Centre")
    assert cached is not None
    assert cached.kind == "station"
    assert cached.value == "Example LIRR Station, NY"
    assert cached.lat is None
    assert cached.lon is None
    assert cached.source == "llm"


def test_full_plan_with_nested_route_round_trip(tmp_db_path) -> None:
    """A Plan with a Route containing multiple TransitLegs round-trips fully."""
    store = Store(tmp_db_path)
    store.init_schema()

    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=3)
    event = Event(
        id="evt-full",
        calendar_id="cal-full",
        calendar_name="Full Test",
        title="Full Round-Trip Event",
        start=start,
        end=start + timedelta(hours=2),
        location_raw="Example University",
        location_resolved=ResolvedLocation(
            kind="station",
            value="Example LIRR Station, NY",
            lat=40.6620,
            lon=-73.6310,
            source="llm",
        ),
    )
    route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="LIRR",
                line="Atlantic Branch",
                headsign="Example Centre",
                depart_at=start - timedelta(minutes=52),
                arrive_at=start - timedelta(minutes=3),
                duration_seconds=2940,
                summary="LIRR Atlantic Branch from Atlantic Terminal to Example Centre",
            ),
            TransitLeg(
                mode="WALKING",
                depart_at=start - timedelta(minutes=3),
                arrive_at=start,
                duration_seconds=180,
                summary="Walk from station to venue",
            ),
        ],
        depart_at=start - timedelta(minutes=52),
        arrive_at=start,
        total_duration_seconds=3120,
        transfers=0,
    )
    plan = Plan(
        event=event,
        route=route,
        leave_at=start - timedelta(minutes=65),
        prep_at=start - timedelta(minutes=85),
    )
    store.upsert_plan(plan)

    retrieved = store.get_plan("evt-full")
    assert retrieved is not None
    assert len(retrieved.route.legs) == 2
    assert retrieved.route.legs[0].line == "Atlantic Branch"
    assert retrieved.route.legs[1].mode == "WALKING"
    assert retrieved.route.total_duration_seconds == 3120