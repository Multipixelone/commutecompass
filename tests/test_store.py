"""Tests for store.py."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from commutecompass.store import Store
from commutecompass.models import (
    CurrentLocation,
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
    """Make a test event with start time offset from now in NYC timezone.

    Uses a large enough offset to ensure the event always falls in the current
    logical day regardless of time-of-day (accounts for 02:00-01:59 boundary).
    """
    from commutecompass.timeutil import now_nyc

    now = now_nyc()
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

def test_store_init(tmp_db_path: Path) -> None:
    """Store can be instantiated."""
    store = Store(tmp_db_path)
    assert store.db_path == tmp_db_path


def test_store_init_schema_creates_tables(tmp_db_path: Path) -> None:
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


def test_store_init_schema_idempotent(tmp_db_path: Path) -> None:
    """Calling init_schema twice does not raise."""
    store = Store(tmp_db_path)
    store.init_schema()
    store.init_schema()  # should not raise


# ── Plan CRUD tests ────────────────────────────────────────────────────────────

def test_upsert_plan_insert(tmp_db_path: Path) -> None:
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


def test_upsert_plan_replace(tmp_db_path: Path) -> None:
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


def test_get_plan_missing(tmp_db_path: Path) -> None:
    """get_plan returns None for non-existent event."""
    store = Store(tmp_db_path)
    store.init_schema()
    assert store.get_plan("nonexistent") is None


def test_today_plans_returns_today(tmp_db_path: Path) -> None:
    """today_plans returns plans with event_start today in NYC."""
    store = Store(tmp_db_path)
    store.init_schema()

    # Create event starting soon (1h offset) so it always falls within
    # the current logical day regardless of time-of-day.
    event = make_event("evt-today", start_offset_hours=1, end_offset_hours=3)
    plan = make_plan(event)
    store.upsert_plan(plan)

    plans = store.today_plans()
    assert len(plans) >= 1
    assert any(p.event.id == "evt-today" for p in plans)


def test_today_plans_excludes_tomorrow(tmp_db_path: Path) -> None:
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


def test_today_plans_before_2am_includes_0130_as_previous_day(tmp_db_path: Path) -> None:
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


def test_today_plans_after_2am_includes_0230_as_current_day(tmp_db_path: Path) -> None:
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


def test_delete_old_plans(tmp_db_path: Path) -> None:
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

def test_schedule_ping_insert(tmp_db_path: Path) -> None:
    """schedule_ping inserts a ping entry."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-ping-1", fire_offset_minutes=30)
    store.schedule_ping(ping)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(pending) >= 1
    assert any(p.id == "ping-001" for p in pending)


def test_cancel_pings(tmp_db_path: Path) -> None:
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


# ── Ping dedup / idempotency ─────────────────────────────────────────────────────

def test_schedule_ping_dedup_same_event_id_kind(tmp_db_path: Path) -> None:
    """Scheduling the same (event_id, kind) twice results in one row with the later values.

    The runtime pattern is cancel-then-reschedule. After the second schedule call,
    exactly one pending ping exists for that (event_id, kind) pair, holding the
    latest fire_at and message — the original row is replaced, not duplicated.
    """
    store = Store(tmp_db_path)
    store.init_schema()

    now = datetime.now(timezone.utc)
    base = now + timedelta(hours=3)

    # First ping
    ping_a = PingEntry(
        id="ping-first",
        event_id="evt-dedup",
        kind="leave",
        fire_at=base,
        fired=False,
        message="Leave now (original)",
    )
    store.schedule_ping(ping_a)

    # Runtime dedup pattern: cancel then reschedule with updated values
    store.cancel_pings("evt-dedup")
    updated = base + timedelta(minutes=15)
    ping_b = PingEntry(
        id="ping-second",
        event_id="evt-dedup",
        kind="leave",
        fire_at=updated,
        fired=False,
        message="Leave now (updated)",
    )
    store.schedule_ping(ping_b)

    pending = store.pending_pings(now + timedelta(hours=5))
    assert len(pending) == 1, "Expected exactly one pending ping after dedup"
    assert pending[0].id == "ping-second"
    assert pending[0].message == "Leave now (updated)"
    assert pending[0].fire_at == updated


def test_schedule_ping_same_event_different_kinds_both_coexist(tmp_db_path: Path) -> None:
    """Different kinds (prep/leave) for the same event are independent and both remain.

    This guards against accidental cross-kind dedup if the runtime ever tried to
    key pings solely by event_id without considering kind.
    """
    store = Store(tmp_db_path)
    store.init_schema()

    now = datetime.now(timezone.utc)
    prep_fire = now + timedelta(hours=1)
    leave_fire = now + timedelta(hours=2)

    store.schedule_ping(PingEntry(
        id="ping-prep",
        event_id="evt-xy",
        kind="prep",
        fire_at=prep_fire,
        fired=False,
        message="Start prep",
    ))
    store.schedule_ping(PingEntry(
        id="ping-leave",
        event_id="evt-xy",
        kind="leave",
        fire_at=leave_fire,
        fired=False,
        message="Head out",
    ))

    pending = store.pending_pings(now + timedelta(hours=3))
    assert len(pending) == 2
    assert {p.kind for p in pending} == {"prep", "leave"}


def test_pending_pings_filters_fired(tmp_db_path: Path) -> None:
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


def test_mark_fired(tmp_db_path: Path) -> None:
    """mark_fired sets fired=1 and fired_at timestamp."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-fired", fire_offset_minutes=15)
    store.schedule_ping(ping)

    fired_at = datetime.now(timezone.utc)
    store.mark_fired("ping-001", fired_at)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    assert not any(p.id == "ping-001" for p in pending)


def test_pending_pings_respects_before_bound(tmp_db_path: Path) -> None:
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

def test_cache_geocode_insert_and_retrieve(tmp_db_path: Path) -> None:
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


def test_get_geocode_expired(tmp_db_path: Path) -> None:
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


def test_get_geocode_miss(tmp_db_path: Path) -> None:
    """get_geocode returns None for uncached raw strings."""
    store = Store(tmp_db_path)
    store.init_schema()
    assert store.get_geocode("never-cached-address") is None


# ── Route cache tests ───────────────────────────────────────────────────────────

def test_cache_route_round_trip(tmp_db_path: Path) -> None:
    """A cached route is retrievable for the same (origin, dest, mode) triple."""
    store = Store(tmp_db_path)
    store.init_schema()
    route = make_route(datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc))

    store.cache_route("40.6950,-73.9890", "Midtown", "transit", route)
    got = store.get_cached_route("40.6950,-73.9890", "Midtown", "transit")
    assert got is not None
    assert got.total_duration_seconds == route.total_duration_seconds
    # Different mode / dest / origin are cache misses.
    assert store.get_cached_route("40.6950,-73.9890", "Midtown", "driving") is None
    assert store.get_cached_route("40.6950,-73.9890", "Brooklyn", "transit") is None


def test_cache_route_keeps_latest(tmp_db_path: Path) -> None:
    """Re-caching the same triple overwrites the previous route."""
    store = Store(tmp_db_path)
    store.init_schema()
    older = make_route(datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc))
    older.total_duration_seconds = 1000
    newer = make_route(datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc))
    newer.total_duration_seconds = 2000

    store.cache_route("k", "d", "transit", older)
    store.cache_route("k", "d", "transit", newer)
    got = store.get_cached_route("k", "d", "transit")
    assert got is not None
    assert got.total_duration_seconds == 2000


def test_get_cached_route_respects_max_age(tmp_db_path: Path) -> None:
    """A stale cached route past max_age is treated as a miss."""
    store = Store(tmp_db_path)
    store.init_schema()
    route = make_route(datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc))
    store.cache_route("k", "d", "transit", route)
    assert store.get_cached_route("k", "d", "transit", max_age_days=0) is None


# ── Alert ledger tests ─────────────────────────────────────────────────────────

def test_mark_alert_seen_and_is_alert_seen(tmp_db_path: Path) -> None:
    """mark_alert_seen and is_alert_seen round-trip."""
    store = Store(tmp_db_path)
    store.init_schema()

    store.mark_alert_seen("alert-abc", "evt-001")
    assert store.is_alert_seen("alert-abc", "evt-001") is True
    assert store.is_alert_seen("alert-abc", "evt-002") is False
    assert store.is_alert_seen("alert-xyz", "evt-001") is False


def test_mark_alert_seen_idempotent(tmp_db_path: Path) -> None:
    """mark_alert_seen is idempotent — calling twice does not raise."""
    store = Store(tmp_db_path)
    store.init_schema()
    store.mark_alert_seen("alert-dup", "evt-dup")
    store.mark_alert_seen("alert-dup", "evt-dup")  # should not raise


# ── Round-trip integrity tests ─────────────────────────────────────────────────

def test_plan_round_trip_with_datetime_iso8601_offset(tmp_db_path: Path) -> None:
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
    # Stored datetime strings should be ISO-8601 with offset (either + or -)
    assert ("+" in stored_data["event"]["start"] or "-" in stored_data["event"]["start"]) and ("T" in stored_data["event"]["start"])


def test_ping_entry_round_trip_with_datetime(tmp_db_path: Path) -> None:
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
    assert ping.fired_at is not None
    store.mark_fired("ping-rt", ping.fired_at)

    pending = store.pending_pings(datetime.now(timezone.utc) + timedelta(hours=1))
    next((p for p in pending if p.id == "ping-rt"), None)
    # After marking fired it won't appear in pending; verify via direct query
    with __import__("sqlite3").connect(tmp_db_path) as conn:
        row = conn.execute(
            "SELECT fired_at FROM pings WHERE id = ?", ("ping-rt",)
        ).fetchone()
    assert row[0] is not None
    # Verify it's a valid ISO-8601 string with offset
    assert "+" in row[0] or "Z" in row[0]
    datetime.fromisoformat(row[0])  # should not raise


def test_resolved_location_round_trip(tmp_db_path: Path) -> None:
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


def test_full_plan_with_nested_route_round_trip(tmp_db_path: Path) -> None:
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
    assert retrieved.route is not None
    assert len(retrieved.route.legs) == 2
    assert retrieved.route.legs[0].line == "Atlantic Branch"
    assert retrieved.route.legs[1].mode == "WALKING"
    assert retrieved.route.total_duration_seconds == 3120


# ── current_location tests ────────────────────────────────────────────────────


class TestCurrentLocation:
    def test_get_when_empty_returns_none(self, tmp_db_path: Path) -> None:
        store = Store(tmp_db_path)
        store.init_schema()
        assert store.get_current_location() is None

    def test_upsert_then_get_returns_row(self, tmp_db_path: Path) -> None:
        from commutecompass.timeutil import now_nyc

        store = Store(tmp_db_path)
        store.init_schema()
        captured = now_nyc()
        loc = CurrentLocation(
            lat=40.7128,
            lon=-74.006,
            zone="not_home",
            captured_at=captured,
            source="home_assistant",
        )
        store.upsert_current_location(loc)

        got = store.get_current_location()
        assert got is not None
        assert got.lat == 40.7128
        assert got.lon == -74.006
        assert got.zone == "not_home"
        assert got.source == "home_assistant"

    def test_upsert_overwrites_singleton(self, tmp_db_path: Path) -> None:
        from commutecompass.timeutil import now_nyc

        store = Store(tmp_db_path)
        store.init_schema()
        store.upsert_current_location(
            CurrentLocation(lat=1.0, lon=2.0, zone="home", captured_at=now_nyc())
        )
        store.upsert_current_location(
            CurrentLocation(lat=3.0, lon=4.0, zone="not_home", captured_at=now_nyc())
        )

        got = store.get_current_location()
        assert got is not None
        assert got.lat == 3.0
        assert got.lon == 4.0
        assert got.zone == "not_home"

    def test_stale_returns_none_when_max_age_exceeded(self, tmp_db_path: Path) -> None:
        from commutecompass.timeutil import now_nyc

        store = Store(tmp_db_path)
        store.init_schema()
        old = now_nyc() - timedelta(hours=2)
        store.upsert_current_location(
            CurrentLocation(lat=1.0, lon=2.0, zone="not_home", captured_at=old)
        )

        assert store.get_current_location(max_age_minutes=30) is None
        # No TTL → still returns the row
        fresh = store.get_current_location(max_age_minutes=None)
        assert fresh is not None
        assert fresh.lat == 1.0

    def test_accuracy_m_round_trip(self, tmp_db_path: Path) -> None:
        from commutecompass.timeutil import now_nyc

        store = Store(tmp_db_path)
        store.init_schema()
        store.upsert_current_location(
            CurrentLocation(
                lat=1.0,
                lon=2.0,
                zone="not_home",
                captured_at=now_nyc(),
                accuracy_m=15.0,
            )
        )
        got = store.get_current_location()
        assert got is not None
        assert got.accuracy_m == 15.0

    def test_accuracy_m_defaults_to_none(self, tmp_db_path: Path) -> None:
        """upserts that omit accuracy_m should round-trip as None."""
        from commutecompass.timeutil import now_nyc

        store = Store(tmp_db_path)
        store.init_schema()
        store.upsert_current_location(
            CurrentLocation(lat=1.0, lon=2.0, zone="home", captured_at=now_nyc())
        )
        got = store.get_current_location()
        assert got is not None
        assert got.accuracy_m is None

    def test_schema_migration_adds_accuracy_m_to_pre_phase1_db(
        self, tmp_db_path: Path
    ) -> None:
        """init_schema must idempotently add accuracy_m to an older current_location table."""
        import sqlite3

        # Hand-create the pre-migration table.
        tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(tmp_db_path) as conn:
            conn.execute("""
                CREATE TABLE current_location (
                    id TEXT PRIMARY KEY DEFAULT 'singleton',
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    zone TEXT,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL
                )
            """)

        # Run the migration via init_schema.
        store = Store(tmp_db_path)
        store.init_schema()

        with sqlite3.connect(tmp_db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(current_location)").fetchall()}
        assert "accuracy_m" in cols


# ── claim_ping (atomic) tests ──────────────────────────────────────────────────


def test_claim_ping_returns_true_first_time(tmp_db_path: Path) -> None:
    """The first caller transitions fired 0→1 and gets True back."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-claim-1", fire_offset_minutes=-5)
    store.schedule_ping(ping)

    now = datetime.now(timezone.utc)
    assert store.claim_ping(ping.id, now) is True


def test_claim_ping_returns_false_on_second_call(tmp_db_path: Path) -> None:
    """A second claim on the same ping returns False (already fired)."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-claim-2", fire_offset_minutes=-5)
    store.schedule_ping(ping)

    now = datetime.now(timezone.utc)
    assert store.claim_ping(ping.id, now) is True
    # Second claim must NOT re-fire; this is the race protection.
    assert store.claim_ping(ping.id, now) is False


def test_claim_ping_marks_fired_in_db(tmp_db_path: Path) -> None:
    """After a successful claim the row is fired and excluded from pending."""
    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-claim-3", fire_offset_minutes=-5)
    store.schedule_ping(ping)

    now = datetime.now(timezone.utc)
    store.claim_ping(ping.id, now)

    pending = store.pending_pings(before=now + timedelta(hours=1))
    assert not any(p.id == ping.id for p in pending)


def test_is_alert_seen_today_per_day(tmp_db_path: Path) -> None:
    """is_alert_seen_today is true after a same-day mark and false on a fresh DB."""
    store = Store(tmp_db_path)
    store.init_schema()

    assert store.is_alert_seen_today("alert-x") is False
    store.mark_alert_seen("alert-x", "evt-A")
    assert store.is_alert_seen_today("alert-x") is True
    # A different alert is not affected.
    assert store.is_alert_seen_today("alert-y") is False


def test_claim_ping_concurrent_threads_only_one_wins(tmp_db_path: Path) -> None:
    """Two threads racing to claim the same ping: exactly one returns True."""
    import threading

    store = Store(tmp_db_path)
    store.init_schema()
    ping = make_ping("evt-race", fire_offset_minutes=-5)
    store.schedule_ping(ping)

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        results.append(store.claim_ping(ping.id, datetime.now(timezone.utc)))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 1, (
        f"Expected exactly one winner, got {results}"
    )


# ── adjust_log (record_adjust / last_adjust / mark_adjust_undone) ─────────────


def test_record_adjust_with_explicit_key_dedups_on_retry(tmp_db_path: Path) -> None:
    """A second record_adjust with the same key returns None (no-op)."""
    store = Store(tmp_db_path)
    store.init_schema()

    prev = datetime.now(timezone.utc)
    k1 = store.record_adjust("evt-1", add_prep_minutes=30, prev_prep_at=prev, key="k-stable")
    assert k1 == "k-stable"

    k2 = store.record_adjust("evt-1", add_prep_minutes=30, prev_prep_at=prev, key="k-stable")
    assert k2 is None


def test_record_adjust_without_key_generates_auto_key(tmp_db_path: Path) -> None:
    """When key=None each call gets a unique auto-key — no collision."""
    store = Store(tmp_db_path)
    store.init_schema()

    prev = datetime.now(timezone.utc)
    a = store.record_adjust("evt-1", add_prep_minutes=15, prev_prep_at=prev)
    b = store.record_adjust("evt-1", add_prep_minutes=15, prev_prep_at=prev)
    assert a is not None and b is not None and a != b
    assert a.startswith("auto:") and b.startswith("auto:")


def test_last_adjust_returns_most_recent_non_undone(tmp_db_path: Path) -> None:
    """last_adjust ignores undone rows and returns the latest applied_at."""
    store = Store(tmp_db_path)
    store.init_schema()

    base = datetime.now(timezone.utc)
    # Three rows, last-applied wins.
    store.record_adjust("evt-A", add_prep_minutes=10, prev_prep_at=base, key="k1")
    store.record_adjust("evt-A", add_prep_minutes=20, prev_prep_at=base, key="k2")
    store.record_adjust("evt-A", add_prep_minutes=30, prev_prep_at=base, key="k3")

    row = store.last_adjust("evt-A")
    assert row is not None
    assert row.key == "k3"
    assert row.add_prep_minutes == 30

    # Undoing k3 should bubble k2 to the top.
    assert store.mark_adjust_undone("k3") is True
    row = store.last_adjust("evt-A")
    assert row is not None and row.key == "k2"


def test_last_adjust_filters_by_event_id(tmp_db_path: Path) -> None:
    """Passing event_id restricts which adjust history we walk."""
    store = Store(tmp_db_path)
    store.init_schema()

    base = datetime.now(timezone.utc)
    store.record_adjust("evt-A", add_prep_minutes=10, prev_prep_at=base, key="kA")
    store.record_adjust("evt-B", add_prep_minutes=20, prev_prep_at=base, key="kB")

    row = store.last_adjust("evt-A")
    assert row is not None and row.event_id == "evt-A"

    row = store.last_adjust("evt-B")
    assert row is not None and row.event_id == "evt-B"

    row = store.last_adjust(None)
    assert row is not None  # global: either one is acceptable


def test_last_adjust_returns_none_for_legacy_keyless_rows(tmp_db_path: Path) -> None:
    """A row recorded via the legacy record_adjust_key path (no prev_prep_at) is skipped."""
    store = Store(tmp_db_path)
    store.init_schema()

    assert store.record_adjust_key("legacy-k", "evt-legacy") is True
    # No add_prep_minutes / prev_prep_at populated — last_adjust must skip.
    assert store.last_adjust("evt-legacy") is None


def test_mark_adjust_undone_twice_is_noop(tmp_db_path: Path) -> None:
    """A second mark_adjust_undone on the same key returns False."""
    store = Store(tmp_db_path)
    store.init_schema()

    store.record_adjust(
        "evt-X", add_prep_minutes=5, prev_prep_at=datetime.now(timezone.utc), key="k"
    )
    assert store.mark_adjust_undone("k") is True
    assert store.mark_adjust_undone("k") is False


# ── event_mutes (mute_event / is_muted / unmute_event) ────────────────────────


def test_mute_event_with_no_expiry_is_muted_forever(tmp_db_path: Path) -> None:
    """An open-ended mute reports muted=True regardless of wall-clock."""
    store = Store(tmp_db_path)
    store.init_schema()

    assert store.is_muted("evt-1") is False
    store.mute_event("evt-1")
    assert store.is_muted("evt-1") is True


def test_mute_event_with_future_expiry_is_muted_until_then(tmp_db_path: Path) -> None:
    """Setting expires_at in the future keeps the event muted."""
    from commutecompass.timeutil import now_nyc

    store = Store(tmp_db_path)
    store.init_schema()

    future = now_nyc() + timedelta(hours=2)
    store.mute_event("evt-1", expires_at=future)
    assert store.is_muted("evt-1") is True


def test_mute_event_with_past_expiry_reports_unmuted(tmp_db_path: Path) -> None:
    """A row whose expires_at is already in the past does NOT count as muted."""
    from commutecompass.timeutil import now_nyc

    store = Store(tmp_db_path)
    store.init_schema()

    past = now_nyc() - timedelta(seconds=10)
    store.mute_event("evt-1", expires_at=past)
    assert store.is_muted("evt-1") is False


def test_unmute_event_removes_mute(tmp_db_path: Path) -> None:
    """unmute_event drops the row so subsequent is_muted is False."""
    store = Store(tmp_db_path)
    store.init_schema()

    store.mute_event("evt-1")
    assert store.is_muted("evt-1") is True

    removed = store.unmute_event("evt-1")
    assert removed == 1
    assert store.is_muted("evt-1") is False


def test_mute_event_overwrites_previous_expiry(tmp_db_path: Path) -> None:
    """Re-muting the same event updates expires_at in place."""
    from commutecompass.timeutil import now_nyc

    store = Store(tmp_db_path)
    store.init_schema()

    past = now_nyc() - timedelta(seconds=10)
    store.mute_event("evt-1", expires_at=past)
    assert store.is_muted("evt-1") is False

    # Re-mute forever — the past expiry must be overwritten.
    store.mute_event("evt-1")
    assert store.is_muted("evt-1") is True


# ── get_pending_ping ───────────────────────────────────────────────────────────


def test_get_pending_ping_returns_unfired_row(tmp_db_path: Path) -> None:
    """The unfired prep ping for an event is round-tripped."""
    store = Store(tmp_db_path)
    store.init_schema()

    ping = make_ping("evt-snooze", fire_offset_minutes=30)
    store.schedule_ping(ping)

    found = store.get_pending_ping("evt-snooze", kind="prep")
    assert found is not None
    assert found.event_id == "evt-snooze"
    assert found.kind == "prep"


def test_get_pending_ping_skips_fired(tmp_db_path: Path) -> None:
    """A fired ping is not returned — even if it's the only row for that event."""
    store = Store(tmp_db_path)
    store.init_schema()

    ping = make_ping("evt-fired", fire_offset_minutes=-5)
    store.schedule_ping(ping)
    store.claim_ping(ping.id, datetime.now(timezone.utc))

    assert store.get_pending_ping("evt-fired", kind="prep") is None


def test_get_pending_ping_missing_returns_none(tmp_db_path: Path) -> None:
    """No row at all → None."""
    store = Store(tmp_db_path)
    store.init_schema()
    assert store.get_pending_ping("nope", kind="prep") is None