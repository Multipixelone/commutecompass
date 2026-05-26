"""Tests for jobs/tomorrow.py — the pull-model HA alarm pusher."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from commutecompass.config import (
    CalendarSpec,
    Config,
    HomeAssistantConfig,
    HomeAssistantTomorrowConfig,
    MtaConfig,
    OpencodeGoConfig,
    Origin,
    PathsConfig,
    PrepConfig,
    SchedulingConfig,
)
from commutecompass.jobs.tomorrow import run as tomorrow_run
from commutecompass.models import Event, Plan, Route, TransitLeg
from commutecompass.timeutil import NYC_TZ, now_nyc


@pytest.fixture
def tomorrow_config(tmp_path: Path) -> Config:
    venues_path = tmp_path / "venues.yaml"
    venues_path.write_text(
        """
- aliases: ["200 Example St"]
  resolves_to:
    kind: address
    value: "200 Example St, New York, NY 10001"
    source: known_venues
"""
    )
    (tmp_path / "token.json").write_text("{}")
    return Config(
        origin=Origin(
            address="123 Example Ave, Brooklyn, NY 11201",
            lat=40.6950,
            lon=-73.9890,
        ),
        calendars=[CalendarSpec(id="test-cal", name="Test", enabled=True)],
        prep=PrepConfig(prep_minutes=20, safety_buffer_minutes=5),
        scheduling=SchedulingConfig(
            morning_run_time=datetime.strptime("06:00", "%H:%M").time(),
            poll_interval_seconds=60,
        ),
        paths=PathsConfig(
            venues_file=str(venues_path),
            db_path=str(tmp_path / "test.db"),
            oauth_token_path=str(tmp_path / "token.json"),
        ),
        opencode_go=OpencodeGoConfig(
            endpoint="https://example/v1/chat/completions",
            model="deepseek-v4-flash",
        ),
        mta=MtaConfig(
            subway_alerts_url="https://x",
            lirr_alerts_url="https://x",
            bus_alerts_url="https://x",
        ),
        home_assistant=HomeAssistantConfig(
            enabled=True,
            base_url="http://ha.local:8123",
            tomorrow=HomeAssistantTomorrowConfig(
                enabled=True,
                script="script.commute_set_tomorrow_alarm",
            ),
        ),
        google_maps_api_key="k",
        google_oauth_client_secret_json="{}",
        opencode_go_token="t",
        home_assistant_token="ha-tok",
    )


def _tomorrow_events() -> list[Event]:
    now = now_nyc()
    # Anchor to a fixed wall-clock time tomorrow to avoid DST flake.
    base = (now + timedelta(days=1)).astimezone(NYC_TZ).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return [
        Event(
            id="evt-late",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Afternoon meeting",
            start=base + timedelta(hours=5),
            end=base + timedelta(hours=6),
            location_raw="200 Example St",
        ),
        Event(
            id="evt-early",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Morning class",
            start=base,
            end=base + timedelta(hours=2),
            location_raw="200 Example St",
        ),
        Event(
            id="evt-no-loc",
            calendar_id="test-cal",
            calendar_name="Test",
            title="No location",
            start=base + timedelta(hours=2),
            end=base + timedelta(hours=3),
            location_raw=None,
        ),
    ]


def _route() -> Route:
    now = now_nyc()
    return Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton",
                depart_at=now,
                arrive_at=now + timedelta(minutes=40),
                duration_seconds=2400,
                summary="C train",
            ),
        ],
        depart_at=now,
        arrive_at=now + timedelta(minutes=40),
        total_duration_seconds=2400,
        transfers=0,
    )


def test_picks_earliest_prep_and_pushes(tomorrow_config: Config) -> None:
    """Earliest non-errored prep_at is selected; HA push receives ISO datetime."""
    events = _tomorrow_events()
    route = _route()

    late_prep = events[0].start - timedelta(minutes=45)
    early_prep = events[1].start - timedelta(minutes=65)

    def fake_plan(event: Event, **_: Any) -> Plan:
        if event.id == "evt-late":
            return Plan(
                event=event,
                route=route,
                leave_at=events[0].start - timedelta(minutes=25),
                prep_at=late_prep,
            )
        if event.id == "evt-early":
            return Plan(
                event=event,
                route=route,
                leave_at=events[1].start - timedelta(minutes=45),
                prep_at=early_prep,
            )
        return Plan(event=event, error="location_unresolved")

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm",
        return_value=True,
    ) as mock_push, patch(
        "commutecompass.ha_client.fetch_zones", return_value={}
    ):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config)

    assert result is not None
    assert result.event.id == "evt-early"
    assert result.prep_at == early_prep

    mock_push.assert_called_once()
    args, kwargs = mock_push.call_args
    assert args[0] == "http://ha.local:8123"
    assert args[1] == "ha-tok"
    assert args[2] == "script.commute_set_tomorrow_alarm"
    assert args[3] == early_prep


def test_fetch_window_is_next_logical_day(tomorrow_config: Config) -> None:
    """Calendar fetch uses next-day logical bounds, not today's."""
    from commutecompass.timeutil import logical_day_bounds_nyc

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm", return_value=True
    ), patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = []
        tomorrow_run(tomorrow_config)

    expected_start, expected_end = logical_day_bounds_nyc(
        now_nyc() + timedelta(days=1)
    )
    call_kwargs = mock_cal.return_value.fetch_events.call_args.kwargs
    assert call_kwargs["start"] == expected_start
    assert call_kwargs["end"] == expected_end


def test_no_events_skips_push(tomorrow_config: Config) -> None:
    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = []
        result = tomorrow_run(tomorrow_config)

    assert result is None
    mock_push.assert_not_called()


def test_all_plans_errored_skips_push(tomorrow_config: Config) -> None:
    events = _tomorrow_events()

    def fake_plan(event: Event, **_: Any) -> Plan:
        return Plan(event=event, error="location_unresolved")

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config)

    assert result is None
    mock_push.assert_not_called()


def test_disabled_tomorrow_skips_push_but_returns_chosen(
    tomorrow_config: Config,
) -> None:
    """When [home_assistant.tomorrow].enabled=false we still plan + log but don't POST."""
    tomorrow_config.home_assistant.tomorrow.enabled = False

    events = _tomorrow_events()[:1]
    route = _route()
    prep = events[0].start - timedelta(minutes=45)

    def fake_plan(event: Event, **_: Any) -> Plan:
        return Plan(
            event=event,
            route=route,
            leave_at=events[0].start - timedelta(minutes=25),
            prep_at=prep,
        )

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config)

    assert result is not None
    assert result.prep_at == prep
    mock_push.assert_not_called()


def test_dry_run_skips_push(tomorrow_config: Config) -> None:
    events = _tomorrow_events()[:1]
    route = _route()
    prep = events[0].start - timedelta(minutes=45)

    def fake_plan(event: Event, **_: Any) -> Plan:
        return Plan(
            event=event,
            route=route,
            leave_at=events[0].start - timedelta(minutes=25),
            prep_at=prep,
        )

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config, dry_run=True)

    assert result is not None
    mock_push.assert_not_called()


def test_calendar_fetch_failure_returns_none(tomorrow_config: Config) -> None:
    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal:
        mock_cal.return_value.fetch_events.side_effect = RuntimeError("api down")
        result = tomorrow_run(tomorrow_config)

    assert result is None


def test_plan_failure_doesnt_kill_run(tomorrow_config: Config) -> None:
    """One event's plan_event raising still lets the rest contribute."""
    events = _tomorrow_events()[:2]
    route = _route()
    early_prep = events[1].start - timedelta(minutes=65)

    def fake_plan(event: Event, **_: Any) -> Plan:
        if event.id == "evt-late":
            raise RuntimeError("blew up")
        return Plan(
            event=event,
            route=route,
            leave_at=events[1].start - timedelta(minutes=45),
            prep_at=early_prep,
        )

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm", return_value=True
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config)

    assert result is not None
    assert result.event.id == "evt-early"
    mock_push.assert_called_once()


def test_morning_pings_not_touched(tomorrow_config: Config) -> None:
    """Tomorrow job must not upsert plans or schedule pings into today's store."""
    from commutecompass.models import PingEntry
    from commutecompass.store import Store

    store = Store(tomorrow_config.paths.db_path)
    store.init_schema()
    # Pre-existing today plan + ping
    today_event = _tomorrow_events()[0]
    today_plan = Plan(
        event=today_event,
        route=_route(),
        leave_at=now_nyc() + timedelta(hours=1),
        prep_at=now_nyc() + timedelta(minutes=30),
    )
    store.upsert_plan(today_plan)
    assert today_plan.prep_at is not None
    store.schedule_ping(
        PingEntry(
            id="keep-me",
            event_id=today_event.id,
            kind="prep",
            fire_at=today_plan.prep_at,
            message="don't touch me",
        )
    )

    events = _tomorrow_events()
    route = _route()
    prep = events[1].start - timedelta(minutes=65)

    def fake_plan(event: Event, **_: Any) -> Plan:
        return Plan(
            event=event,
            route=route,
            leave_at=events[1].start - timedelta(minutes=45),
            prep_at=prep,
        )

    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event", side_effect=fake_plan
    ), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm", return_value=True
    ), patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        tomorrow_run(tomorrow_config)

    # Today's ping survived (tomorrow job didn't replace/cancel it)
    pending = store.pending_pings(before=now_nyc() + timedelta(days=2))
    assert any(p.id == "keep-me" for p in pending)
