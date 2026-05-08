"""Tests for morning job (§6.14)."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from commutecop.jobs.morning import run
from commutecop.models import (
    Alert,
    CalendarSpec,
    Config,
    Event,
    Origin,
    PathsConfig,
    PingEntry,
    Plan,
    PrepConfig,
    ResolvedLocation,
    Route,
    SchedulingConfig,
    OpencodeGoConfig,
    MtaConfig,
    TransitLeg,
)
from commutecop.store import Store
from commutecop.timeutil import NYC_TZ, now_nyc


# ─────────── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def minimal_config(tmp_path: Path) -> Config:
    """Minimal config for testing."""
    db_path = tmp_path / "test.db"
    venues_path = tmp_path / "venues.yaml"
    oauth_path = tmp_path / "token.json"

    venues_path.write_text(
        """
- aliases: ["200 Example St", "Example School"]
  resolves_to:
    kind: address
    value: "200 Example St, New York, NY 10001"
    source: known_venues
"""
    )
    oauth_path.write_text("{}")

    return Config(
        origin=Origin(
            address="123 Example Ave, Brooklyn, NY 11201",
            lat=40.6950,
            lon=-73.9890,
            subway_station="Jay St-MetroTech",
            lirr_station="Atlantic Terminal",
        ),
        calendars=[
            CalendarSpec(id="test-cal", name="Test", enabled=True),
        ],
        prep=PrepConfig(prep_minutes=20, safety_buffer_minutes=5),
        scheduling=SchedulingConfig(
            morning_run_time=datetime.strptime("06:00", "%H:%M").time(),
            poll_interval_seconds=60,
        ),
        paths=PathsConfig(
            venues_file=str(venues_path),
            db_path=str(db_path),
            oauth_token_path=str(oauth_path),
        ),
        opencode_go=OpencodeGoConfig(
            endpoint="https://example/v1/chat/completions",
            model="deepseek-v4-flash",
        ),
        mta=MtaConfig(
            subway_alerts_url="https://subway-alerts.example",
            lirr_alerts_url="https://lirr-alerts.example",
            bus_alerts_url="https://bus-alerts.example",
        ),
        google_maps_api_key="test-key",
        google_oauth_client_secret_json="{}",
        telegram_bot_token="test-token",
        telegram_chat_id=12345,
        opencode_go_token="test-token",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db_path = tmp_path / "test.db"
    s = Store(db_path)
    s.init_schema()
    return s


@pytest.fixture
def today_events() -> list[Event]:
    """Two events today: one with location, one without."""
    now = now_nyc()
    return [
        Event(
            id="evt-1",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Example Class",
            start=(now + timedelta(hours=3)).astimezone(NYC_TZ),
            end=(now + timedelta(hours=5)).astimezone(NYC_TZ),
            location_raw="200 Example St",
            location_resolved=None,
            mode_override=None,
        ),
        Event(
            id="evt-2",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Team Meeting",
            start=(now + timedelta(hours=6)).astimezone(NYC_TZ),
            end=(now + timedelta(hours=7)).astimezone(NYC_TZ),
            location_raw=None,
            location_resolved=None,
            mode_override=None,
        ),
    ]


@pytest.fixture
def sample_route() -> Route:
    """A sample transit route."""
    now = now_nyc()
    return Route(
        legs=[
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=now,
                arrive_at=now + timedelta(minutes=5),
                duration_seconds=300,
                summary="Walk to Jay St-MetroTech",
            ),
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St",
                depart_at=now + timedelta(minutes=5),
                arrive_at=now + timedelta(minutes=35),
                duration_seconds=1800,
                summary="C train to Fulton St",
            ),
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=now + timedelta(minutes=35),
                arrive_at=now + timedelta(minutes=40),
                duration_seconds=300,
                summary="Walk to destination",
            ),
        ],
        depart_at=now,
        arrive_at=now + timedelta(minutes=40),
        total_duration_seconds=2400,
        transfers=0,
        raw_provider_payload=None,
    )


# ─────────── Tests ───────────────────────────────────────────────────────────

def test_morning_run_fetches_and_plans(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Verify the full morning sequence: fetch → plan → upsert → ping schedule → digest."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        # ── CalendarClient mock ─────────────────────────────────────────
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        # ── fetch_alerts mock ───────────────────────────────────────────
        mock_fetch_alerts.return_value = []

        # ── TelegramNotifier mock ───────────────────────────────────────
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        # ── plan_event: return a successful plan for evt-1, error plan for evt-2 ─
        now = now_nyc()
        evt1_plan = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=(now + timedelta(hours=3)) - timedelta(minutes=45),
            prep_at=(now + timedelta(hours=3)) - timedelta(minutes=65),
        )
        evt2_plan = Plan(event=today_events[1], error="location_unresolved")

        def mock_plan_event(
            event, config, venues, store, llm, *, mode_override=None
        ):
            if event.id == "evt-1":
                return evt1_plan
            return evt2_plan

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        # ── Verify calendar_client was called ───────────────────────────
        mock_cal.fetch_events.assert_called_once()
        call_args = mock_cal.fetch_events.call_args
        assert minimal_config.calendars == call_args.kwargs["calendars"]

        # ── Verify store: both plans upserted ───────────────────────────
        store = Store(minimal_config.paths.db_path)
        saved_plan1 = store.get_plan("evt-1")
        saved_plan2 = store.get_plan("evt-2")
        assert saved_plan1 is not None
        assert saved_plan2 is not None

        # ── Verify pings scheduled for evt-1 (evt-2 has no route/leave_at) ─
        pending = store.pending_pings(before=now + timedelta(days=1))
        ping_map = {p.event_id: p for p in pending}

        assert "evt-1" in ping_map
        prep_ping = next(p for p in pending if p.kind == "prep" and p.event_id == "evt-1")
        leave_ping = next(p for p in pending if p.kind == "leave" and p.event_id == "evt-1")
        assert prep_ping.fire_at == evt1_plan.prep_at
        assert leave_ping.fire_at == evt1_plan.leave_at

        # ── Verify no pings for evt-2 (error event) ─────────────────────
        evt2_pings = [p for p in pending if p.event_id == "evt-2"]
        assert evt2_pings == []

        # ── Verify digest was built and sent ─────────────────────────────
        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "Example Class" in digest_text
        assert "Team Meeting" in digest_text


def test_morning_run_skips_past_pings(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Pings with fire_at in the past are not scheduled."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        # Plan where leave_at is already in the past
        past_leave = now - timedelta(minutes=10)
        past_plan = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=past_leave,
            prep_at=past_leave - timedelta(minutes=20),
        )

        def mock_plan_event(event, config, venues, store, llm, *, mode_override=None):
            return past_plan

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        store = Store(minimal_config.paths.db_path)
        pending = store.pending_pings(before=now + timedelta(days=1))
        # No pings should be scheduled because both prep_at and leave_at are in the past
        assert pending == []


def test_morning_run_idempotent(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Re-running overwrites plans cleanly (idempotent)."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal_class.return_value = mock_cal
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )
        plan2 = Plan(event=today_events[1], error="location_unresolved")

        call_count = 0

        def mock_plan_event(event, config, venues, store, llm, *, mode_override=None):
            nonlocal call_count
            call_count += 1
            if event.id == "evt-1":
                return plan1
            return plan2

        # First run: fetch_events returns 2 events
        mock_cal.fetch_events.return_value = today_events

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        # Second run: same events — should overwrite cleanly
        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        # plan_event should have been called 4 times total (2 events × 2 runs)
        assert call_count == 4

        store = Store(minimal_config.paths.db_path)
        saved_plan1 = store.get_plan("evt-1")
        saved_plan2 = store.get_plan("evt-2")
        assert saved_plan1 is not None
        assert saved_plan2 is not None

        import sqlite3
        with sqlite3.connect(minimal_config.paths.db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM pings").fetchone()
            # Should have 4 pings total (prep+leave for evt-1 × 2 runs = 4; evt-2 has none)
            assert rows[0] == 4


def test_morning_run_cancel_stale_pings(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Events removed from the calendar have their pings cancelled."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier
        mock_cal_class.return_value = mock_cal

        now = now_nyc()
        store = Store(minimal_config.paths.db_path)
        store.init_schema()

        # Pre-existing plan + pings for an event that will NOT appear today
        stale_event = Event(
            id="stale-evt",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Old Rehearsal",
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=4),
            location_raw="200 Example St",
        )
        stale_route = sample_route
        stale_plan = Plan(
            event=stale_event,
            route=stale_route,
            leave_at=now + timedelta(hours=2) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=2) - timedelta(minutes=65),
        )
        store.upsert_plan(stale_plan)
        store.schedule_ping(
            PingEntry(
                id="stale-ping-1",
                event_id="stale-evt",
                kind="prep",
                fire_at=stale_plan.prep_at,
                message="old prep",
            )
        )
        store.schedule_ping(
            PingEntry(
                id="stale-ping-2",
                event_id="stale-evt",
                kind="leave",
                fire_at=stale_plan.leave_at,
                message="old leave",
            )
        )

        # First run's events — evt-1 but NOT the stale event
        mock_cal.fetch_events.return_value = [today_events[0]]

        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )

        def mock_plan_event(event, config, venues, store, llm, *, mode_override=None):
            return plan1

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        # Stale pings should be gone
        remaining = store.pending_pings(before=now + timedelta(days=1))
        stale_remaining = [p for p in remaining if p.event_id == "stale-evt"]
        assert stale_remaining == []


def test_morning_run_with_affecting_alerts(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Digest includes affecting MTA alerts."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        # Return an alert affecting the C line
        alert = Alert(
            id="alert-c-1",
            header="C train delays",
            description="Expect delays on the C line",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[
                (now_nyc() - timedelta(hours=1), now_nyc() + timedelta(hours=4))
            ],
            severity="WARNING",
        )
        mock_fetch_alerts.return_value = [alert]

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )
        plan2 = Plan(event=today_events[1], error="location_unresolved")

        def mock_plan_event(event, config, venues, store, llm, *, mode_override=None):
            if event.id == "evt-1":
                return plan1
            return plan2

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            run(minimal_config)

        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "C train delays" in digest_text


def test_morning_run_telegram_failure_is_not_fatal(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Telegram send failure doesn't raise; it logs and continues."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = False  # Telegram failure
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )

        def mock_plan_event(event, config, venues, store, llm, *, mode_override=None):
            return plan1

        with patch("commutecop.jobs.morning.plan_event", side_effect=mock_plan_event):
            # Should NOT raise
            run(minimal_config)

        # Plans still persisted
        store = Store(minimal_config.paths.db_path)
        assert store.get_plan("evt-1") is not None


def test_morning_run_empty_calendar(
    minimal_config: Config,
    tmp_path: Path,
) -> None:
    """Empty calendar: digest sent with 'no events' message."""
    with patch("commutecop.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecop.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecop.jobs.morning.TelegramNotifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = []
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        run(minimal_config)

        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "No events" in digest_text or "today" in digest_text.lower()


# ─────────── Store helper ─────────────────────────────────────────────────────

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commutecop.store import Store as StoreType


def pytest_funcarg__store(tmp_path: Path) -> "StoreType":
    s = Store(tmp_path / "test.db")
    s.init_schema()
    return s
