"""Calendar wake advance never changes routes, manual intent, or nap caps."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_alarm import _commute_plan, _config, _nyc, _plan, _provider

from commutecompass.alarm import build_payload, build_schedule, compute_wake, publish, wake_for_day
from commutecompass.alarm_contract import parse_schedule, serialize_schedule
from commutecompass.alarm_daemon import DaemonState, Inputs, Settings, StartRing, step
from commutecompass.alarm_state import ManualWake
from commutecompass.config import AlarmWindowConfig
from commutecompass.models import Plan
from commutecompass.timeutil import NYC_TZ, logical_day_bounds_nyc


def test_fixed_plan_advances_whole_window_but_not_route_or_nap_cap(tmp_path: Path) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(first_alarm_lead_minutes=60))
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 13))
    original = plan.model_dump()
    now = _nyc(2026, 10, 15, 5)
    provider = _provider({date(2026, 10, 15): [plan]})
    before = compute_wake(cfg, now, provider)
    cfg.alarm.window.calendar_advance_minutes = 15
    after = compute_wake(cfg, now, provider)
    assert before.wake_at == _nyc(2026, 10, 15, 8)
    assert before.deadline_at == _nyc(2026, 10, 15, 9)
    assert after.wake_at == _nyc(2026, 10, 15, 7, 45)
    assert after.deadline_at == _nyc(2026, 10, 15, 8, 45)
    assert after.next_prep_at == before.next_prep_at == before.wake_at
    assert plan.model_dump() == original
    assert plan.leave_at == _nyc(2026, 10, 15, 9)
    assert plan.depart_at == _nyc(2026, 10, 15, 9, 13)

    for result, lights in ((before, _nyc(2026, 10, 15, 7, 50)),
                           (after, _nyc(2026, 10, 15, 7, 35))):
        payload = build_payload(result, window=True)
        schedule = parse_schedule(
            serialize_schedule(build_schedule(result, cfg, issued_at=now), first_lead_seconds=3600),
            first_lead_seconds=3600,
        )
        assert isinstance(payload["wake_at"], str)
        assert isinstance(payload["deadline_at"], str)
        assert schedule.wake_at == datetime.fromisoformat(payload["wake_at"]).astimezone(UTC)
        assert schedule.deadline_at == datetime.fromisoformat(payload["deadline_at"]).astimezone(UTC)
        assert schedule.next_prep_at == result.next_prep_at
        assert schedule.deadline_at is not None and schedule.wake_at is not None
        assert schedule.deadline_at - schedule.wake_at == timedelta(minutes=60)
        # HA derives lights from the published deadline, with no second advance.
        assert schedule.deadline_at - timedelta(minutes=70) == lights.astimezone(UTC)

    inputs = Inputs(ha_ok=True, next_alarm=after.wake_at, kind="wake",
                    wake_at=after.wake_at, wake_known=True,
                    deadline_at=after.deadline_at, deadline_known=True)
    settings = Settings.from_config(cfg.alarm)
    state, _ = step(DaemonState(), after.wake_at - timedelta(seconds=1), inputs, settings)
    state, actions = step(state, after.wake_at, inputs, settings)
    assert state.phase == "window" and any(isinstance(a, StartRing) for a in actions)
    assert after.deadline_at is not None
    # A snooze just before final is capped at the advanced deadline.
    pressed = after.deadline_at - timedelta(minutes=1)
    state, _ = step(state, pressed, replace(inputs, snooze_pressed_at=pressed), settings)
    assert state.snooze_until == after.deadline_at
    state, actions = step(state, after.deadline_at, inputs, settings)
    assert state.phase == "final" and state.mode == "ringing"
    assert any(isinstance(a, StartRing) and a.profile is None for a in actions)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("manual_minute", [40, 45, 50])
def test_manual_competes_with_advanced_deadline(
    tmp_path: Path, force: bool, manual_minute: int,
) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, calendar_advance_minutes=15,
    ))
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 13))
    manual = ManualWake(_nyc(2026, 10, 15, 8, manual_minute), force)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]}), manual)
    calendar_wins = manual_minute > 45 and not force
    expected = _nyc(2026, 10, 15, 8, 45) if calendar_wins else manual.deadline_at
    assert result.deadline_at == expected
    assert result.wake_at.astimezone(UTC) == expected.astimezone(UTC) - timedelta(hours=1)
    assert result.source == ("calendar" if calendar_wins else "manual")
    assert bool(result.warning and "conflicts" in result.warning) == (manual_minute > 45)


def test_manual_exact_deadline_with_calendar_advance(tmp_path: Path) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, calendar_advance_minutes=15,
    ))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5), _provider({}),
                          ManualWake(_nyc(2026, 10, 15, 9)))
    assert result.source == "manual"
    assert result.wake_at == _nyc(2026, 10, 15, 8)
    assert result.deadline_at == _nyc(2026, 10, 15, 9)


@pytest.mark.parametrize("kind", ["fallback", "prep", "disabled"])
def test_non_window_wakes_and_nap_caps_unchanged(tmp_path: Path, kind: str) -> None:
    cfg = _config(tmp_path)
    plans = ([] if kind == "fallback" else [_plan(_nyc(2026, 10, 15, 8))]
             if kind == "prep" else [_commute_plan(_nyc(2026, 10, 15, 9))])
    cfg.alarm.window.enabled = kind != "disabled"
    provider = _provider({date(2026, 10, 15): plans})
    before = compute_wake(cfg, _nyc(2026, 10, 15, 5), provider)
    cfg.alarm.window.calendar_advance_minutes = 15
    assert compute_wake(cfg, _nyc(2026, 10, 15, 5), provider) == before


@pytest.mark.parametrize("deadline", [
    datetime(2026, 3, 8, 3, 10, tzinfo=NYC_TZ),
    datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=0),
    datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=1),
])
def test_advance_is_absolute_across_dst(tmp_path: Path, deadline: datetime) -> None:
    cfg = _config(tmp_path, fallback_time="01:59", window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, calendar_advance_minutes=15,
    ))
    plan = _commute_plan((deadline.astimezone(UTC) + timedelta(minutes=13)).astimezone(NYC_TZ))
    # Own the event by its start, even when the advanced wake crosses a day boundary.
    day, _ = logical_day_bounds_nyc(plan.event.start)
    wake, selected = wake_for_day(day, [plan], cfg)
    assert selected is plan
    assert wake.astimezone(UTC) == deadline.astimezone(UTC) - timedelta(minutes=75)


def test_advance_elapsed_today_rolls_to_next_logical_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, calendar_advance_minutes=15,
    ))
    today = _commute_plan(_nyc(2026, 10, 15, 9, 13))
    tomorrow = _commute_plan(_nyc(2026, 10, 16, 9, 13))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 7, 50), _provider({
        date(2026, 10, 15): [today], date(2026, 10, 16): [tomorrow],
    }))
    assert result.day == date(2026, 10, 16)
    assert result.wake_at == _nyc(2026, 10, 16, 7, 45)
    assert result.next_prep_at == _nyc(2026, 10, 15, 8)


@pytest.mark.parametrize("force", [False, True])
def test_retained_effective_deadline_is_not_advanced_twice(tmp_path: Path, force: bool) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, calendar_advance_minutes=15,
    ))
    now = _nyc(2026, 10, 15, 5)
    with patch("commutecompass.ha_client.call_service", return_value=True):
        first, ok = publish(cfg, now=now, plans_for_day=_provider({
            date(2026, 10, 15): [_commute_plan(_nyc(2026, 10, 15, 9, 13))],
        }))
    assert ok

    def broken(day: datetime) -> list[Plan]:
        raise RuntimeError("offline fixture")

    retained = compute_wake(cfg, now, broken)
    assert retained.source == "retained"
    assert retained.wake_at == first.wake_at
    assert retained.deadline_at == first.deadline_at
    manual = ManualWake(_nyc(2026, 10, 15, 8, 50), force)
    result = compute_wake(cfg, now, broken, manual)
    assert result.deadline_at == (manual.deadline_at if force else first.deadline_at)
    assert result.source == ("manual" if force else "retained")
