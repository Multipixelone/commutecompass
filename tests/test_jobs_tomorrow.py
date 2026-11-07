"""Tests for jobs/tomorrow.py — the pull-model HA alarm pusher."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
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
    """A complete plan publishes its earliest prep_at as an ISO datetime."""
    events = _tomorrow_events()[:2]
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
    args, _kwargs = mock_push.call_args
    assert args[0] == "http://ha.local:8123"
    assert args[1] == "ha-tok"
    assert args[2] == "script.commute_set_tomorrow_alarm"
    assert args[3] == early_prep


@pytest.mark.parametrize(
    ("frozen", "expected_start", "expected_end"),
    [
        ("2026-03-07T21:00:00-05:00", "2026-03-08T02:00:00-05:00",
         "2026-03-09T01:59:59.999999-04:00"),
        ("2026-03-08T21:00:00-04:00", "2026-03-09T02:00:00-04:00",
         "2026-03-10T01:59:59.999999-04:00"),
        ("2026-10-31T21:00:00-04:00", "2026-11-01T02:00:00-05:00",
         "2026-11-02T01:59:59.999999-05:00"),
        ("2026-11-01T21:00:00-05:00", "2026-11-02T02:00:00-05:00",
         "2026-11-03T01:59:59.999999-05:00"),
        # Before the 02:00 boundary, tomorrow is still the current calendar date.
        ("2026-11-01T01:30:00-04:00", "2026-11-01T02:00:00-05:00",
         "2026-11-02T01:59:59.999999-05:00"),
        ("2026-11-01T01:30:00-05:00", "2026-11-01T02:00:00-05:00",
         "2026-11-02T01:59:59.999999-05:00"),
    ],
)
def test_fetch_window_is_next_logical_day(
    tomorrow_config: Config, frozen: str, expected_start: str, expected_end: str,
) -> None:
    """Pin NYC calendar boundaries across DST without mirroring job arithmetic."""
    clock = datetime.fromisoformat(frozen).astimezone(NYC_TZ)
    with patch("commutecompass.jobs.tomorrow.now_nyc", return_value=clock), patch(
        "commutecompass.jobs.tomorrow.CalendarClient"
    ) as mock_cal, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = []
        tomorrow_run(tomorrow_config)

    call_kwargs = mock_cal.return_value.fetch_events.call_args.kwargs
    assert call_kwargs["start"].isoformat() == expected_start
    assert call_kwargs["end"].isoformat() == expected_end


@pytest.mark.parametrize("contract", ["legacy", "v1"])
def test_warns_when_legacy_tomorrow_push_and_v1_publisher_are_enabled(
    tomorrow_config: Config, caplog: pytest.LogCaptureFixture, contract: Literal["legacy", "v1"],
) -> None:
    from commutecompass.config import AlarmConfig

    config = tomorrow_config.model_copy(update={
        "alarm": AlarmConfig(enabled=True, schedule_contract=contract),
    })
    with patch("commutecompass.jobs.tomorrow.plan_window", return_value=[]), patch(
        "commutecompass.jobs.tomorrow._publish_alarm_safe"
    ):
        tomorrow_run(config)
    assert ("legacy alarm_at payload" in caplog.text) is (contract == "v1")


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


def test_plan_failure_skips_partial_prep_push_but_publishes_safe_wake(tomorrow_config: Config) -> None:
    """Legacy prep push rejects partial plans; wake publication uses safety."""
    events = _tomorrow_events()[:2]
    route = _route()
    early_prep = events[1].start - timedelta(minutes=65)
    tomorrow_config.alarm.enabled = True
    clock = (events[0].start - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    expected_fallback = events[0].start.replace(hour=12, minute=0, second=0, microsecond=0)

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
    ) as mock_push, patch("commutecompass.ha_client.fetch_zones", return_value={}), patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ) as publish_wake, patch("commutecompass.alarm.fetch_entity_state", return_value=None), patch(
        "commutecompass.notify.build_notifier",
    ) as notifier, patch("commutecompass.jobs.tomorrow.now_nyc", return_value=clock), patch(
        "commutecompass.alarm.now_nyc", return_value=clock,
    ):
        mock_cal.return_value.fetch_events.return_value = events
        result = tomorrow_run(tomorrow_config)

    assert result is None
    mock_push.assert_not_called()
    publish_wake.assert_called_once()
    expected_first = expected_fallback.replace(hour=11, minute=23)
    payload = publish_wake.call_args.kwargs["data"]
    assert payload["wake_at"] == expected_first.isoformat()
    assert payload["deadline_at"] == expected_fallback.isoformat()
    notifier.return_value.send.assert_called_once()


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


def test_alarm_publish_called_when_enabled(tomorrow_config: Config) -> None:
    """[alarm].enabled → tomorrow job re-publishes the wake with its plans."""
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    events = _tomorrow_events()
    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.jobs.tomorrow.plan_event",
        side_effect=lambda event, **_: Plan(event=event, error="location_unresolved"),
    ), patch("commutecompass.jobs.tomorrow.push_tomorrow_alarm"), patch(
        "commutecompass.alarm.publish", side_effect=RuntimeError("HA down")
    ) as mock_pub, patch("commutecompass.ha_client.fetch_zones", return_value={}):
        mock_cal.return_value.fetch_events.return_value = events
        tomorrow_run(tomorrow_config)  # must not raise

    mock_pub.assert_called_once()
    planned = mock_pub.call_args.kwargs["planned"]
    assert len(next(iter(planned.values()))) == len(events)


def test_alarm_publish_not_called_when_disabled(tomorrow_config: Config) -> None:
    with patch("commutecompass.jobs.tomorrow.CalendarClient") as mock_cal, patch(
        "commutecompass.alarm.publish"
    ) as mock_pub:
        mock_cal.return_value.fetch_events.return_value = []
        tomorrow_run(tomorrow_config)
    mock_pub.assert_not_called()


def test_partial_calendar_failure_does_not_publish(tomorrow_config: Config) -> None:
    """A failure after fetching one calendar is not a verified empty day."""
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    tomorrow_config.calendars.append(CalendarSpec(id="second", name="Second"))
    with patch("commutecompass.jobs.tomorrow.CalendarClient") as calendar, patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as legacy, patch("commutecompass.ha_client.call_service") as alarm:
        calendar.return_value.fetch_events.side_effect = RuntimeError("second calendar failed")
        assert tomorrow_run(tomorrow_config) is None
    legacy.assert_not_called()
    alarm.assert_not_called()


def test_partial_plan_failure_skips_prep_push_but_publishes_fallback(tomorrow_config: Config) -> None:
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    now = datetime(2026, 10, 15, 20, tzinfo=NYC_TZ)
    events = [event.model_copy(update={
        "start": datetime(2026, 10, 16, hour, tzinfo=NYC_TZ),
        "end": datetime(2026, 10, 16, hour + 1, tzinfo=NYC_TZ),
    }) for event, hour in zip(_tomorrow_events()[:2], [14, 9], strict=True)]
    plans = [
        Plan(event=events[0], error="no_route"),
        Plan(event=events[1], prep_at=events[1].start - timedelta(minutes=30)),
    ]
    with patch("commutecompass.jobs.tomorrow.plan_window", return_value=plans), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm"
    ) as legacy, patch("commutecompass.ha_client.call_service", return_value=True) as alarm, patch(
        "commutecompass.jobs.tomorrow.now_nyc", return_value=now,
    ), patch("commutecompass.alarm.now_nyc", return_value=now), patch(
        "commutecompass.alarm.fetch_entity_state", return_value=None,
    ), patch("commutecompass.notify.build_notifier") as notifier:
        assert tomorrow_run(tomorrow_config) is None
    legacy.assert_not_called()
    alarm.assert_called_once()
    assert alarm.call_args.kwargs["data"]["wake_at"] == datetime(
        2026, 10, 16, 11, 23, tzinfo=NYC_TZ,
    ).isoformat()
    assert alarm.call_args.kwargs["data"]["deadline_at"] == datetime(
        2026, 10, 16, 12, tzinfo=NYC_TZ,
    ).isoformat()
    notifier.return_value.send.assert_called_once()


def test_evening_job_publishes_despite_historical_too_imminent(tomorrow_config: Config) -> None:
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    now = datetime(2026, 10, 15, 20, tzinfo=NYC_TZ)
    event = _tomorrow_events()[1].model_copy(update={
        "start": datetime(2026, 10, 16, 9, tzinfo=NYC_TZ),
        "end": datetime(2026, 10, 16, 10, tzinfo=NYC_TZ),
    })
    future = Plan(event=event, prep_at=event.start - timedelta(hours=1))
    past_event = event.model_copy(update={
        "start": event.start - timedelta(days=1),
        "end": event.end - timedelta(days=1),
    })
    past = Plan(
        event=past_event, prep_at=past_event.start - timedelta(hours=1),
        leave_at=past_event.start - timedelta(minutes=30), error="too_imminent",
    )
    with patch("commutecompass.jobs.tomorrow.now_nyc", return_value=now), patch(
        "commutecompass.alarm.now_nyc", return_value=now
    ), patch("commutecompass.jobs.tomorrow.plan_window", side_effect=[[future], [past]]), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm", return_value=True
    ), patch("commutecompass.ha_client.call_service", return_value=True) as alarm:
        assert tomorrow_run(tomorrow_config) == future
    alarm.assert_called_once()
    assert future.prep_at is not None
    assert alarm.call_args.kwargs["data"]["wake_at"] == future.prep_at.isoformat()


@pytest.mark.parametrize("buffer_minutes", [5, 15, 21], ids=["safety", "weather", "realtime"])
def test_evening_job_publishes_buffered_leave_deadline(
    tomorrow_config: Config, buffer_minutes: int,
) -> None:
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    now = datetime(2026, 10, 15, 20, tzinfo=NYC_TZ)
    depart = datetime(2026, 10, 16, 9, tzinfo=NYC_TZ)
    leave = depart - timedelta(minutes=buffer_minutes)
    event = _tomorrow_events()[1].model_copy(update={
        "start": depart + timedelta(hours=1), "end": depart + timedelta(hours=2),
    })
    plan = Plan(
        event=event, depart_at=depart, leave_at=leave,
        prep_at=leave - timedelta(minutes=20), leave_buffer_minutes=buffer_minutes,
    )
    with patch("commutecompass.jobs.tomorrow.now_nyc", return_value=now), patch(
        "commutecompass.alarm.now_nyc", return_value=now
    ), patch("commutecompass.jobs.tomorrow.plan_window", side_effect=[[plan], []]), patch(
        "commutecompass.jobs.tomorrow.push_tomorrow_alarm", return_value=True
    ), patch("commutecompass.ha_client.call_service", return_value=True) as alarm:
        assert tomorrow_run(tomorrow_config) == plan
    alarm.assert_called_once()
    payload = alarm.call_args.kwargs["data"]
    assert payload["deadline_at"] == leave.isoformat()
    assert payload["wake_at"] == (leave - timedelta(minutes=37)).isoformat()
    assert payload["next_prep_at"] == payload["wake_at"]
    assert "departure_at" not in payload


def test_verified_empty_tomorrow_publishes_fallback(tomorrow_config: Config) -> None:
    from commutecompass.config import AlarmConfig

    tomorrow_config.alarm = AlarmConfig(enabled=True)
    now = datetime(2026, 10, 15, 20, tzinfo=NYC_TZ)
    with patch("commutecompass.jobs.tomorrow.now_nyc", return_value=now), patch(
        "commutecompass.alarm.now_nyc", return_value=now
    ), patch("commutecompass.jobs.tomorrow.plan_window", return_value=[]), patch(
        "commutecompass.ha_client.call_service", return_value=True
    ) as alarm:
        assert tomorrow_run(tomorrow_config) is None
    alarm.assert_called_once()
    assert alarm.call_args.kwargs["data"] == {
        "wake_at": datetime(2026, 10, 16, 11, 23, tzinfo=NYC_TZ).isoformat(),
        "next_prep_at": "",
        "next_event": "",
        "deadline_at": datetime(2026, 10, 16, 12, tzinfo=NYC_TZ).isoformat(),
    }
