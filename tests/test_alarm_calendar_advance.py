"""Shared wake advance shifts normal windows, never routes, manual intent or nap caps."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_alarm import _commute_plan, _config, _FakeHA, _nyc, _plan, _provider
from test_alarm_window import ProfilePlayer

from commutecompass.alarm import build_payload, build_schedule, compute_wake, publish, wake_for_day
from commutecompass.alarm_contract import parse_schedule, serialize_schedule
from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    DaemonState,
    Inputs,
    Settings,
    StartRing,
    inputs_from_states,
    step,
)
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
    cfg = _config(tmp_path, window=AlarmWindowConfig.model_validate({
        "first_alarm_lead_minutes": 60, "calendar_advance_minutes": 15,
    }))
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
    cfg = _config(tmp_path, window=AlarmWindowConfig.model_validate({
        "first_alarm_lead_minutes": 60, "calendar_advance_minutes": 15,
    }))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5), _provider({}),
                          ManualWake(_nyc(2026, 10, 15, 9)))
    assert result.source == "manual"
    assert result.wake_at == _nyc(2026, 10, 15, 8)
    assert result.deadline_at == _nyc(2026, 10, 15, 9)


@pytest.mark.parametrize("kind", ["disabled_fallback", "prep", "disabled"])
def test_non_window_wakes_and_nap_caps_unchanged(tmp_path: Path, kind: str) -> None:
    cfg = _config(tmp_path)
    plans = ([] if kind == "disabled_fallback" else [_plan(_nyc(2026, 10, 15, 8))]
             if kind == "prep" else [_commute_plan(_nyc(2026, 10, 15, 9))])
    cfg.alarm.window.enabled = kind == "prep"
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
    cfg = _config(tmp_path, fallback_time="12:00", window=AlarmWindowConfig.model_validate({
        "first_alarm_lead_minutes": 60, "calendar_advance_minutes": 15,
    }))
    plan = _commute_plan((deadline.astimezone(UTC) + timedelta(minutes=13)).astimezone(NYC_TZ))
    # A later event owns this logical day even when travel/wake spans its boundary.
    # Keep fallback later than both folds so this isolates calendar arithmetic.
    plan.event.start = deadline.replace(hour=10, minute=0, fold=0)
    plan.event.end = deadline.replace(hour=11, minute=0, fold=0)
    day, _ = logical_day_bounds_nyc(plan.event.start)
    wake, selected = wake_for_day(day, [plan], cfg)
    assert selected is plan
    assert wake.astimezone(UTC) == deadline.astimezone(UTC) - timedelta(minutes=75)


def test_advance_elapsed_today_rolls_to_next_logical_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig.model_validate({
        "first_alarm_lead_minutes": 60, "calendar_advance_minutes": 15,
    }))
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
    cfg = _config(tmp_path, window=AlarmWindowConfig.model_validate({
        "first_alarm_lead_minutes": 60, "calendar_advance_minutes": 15,
    }))
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


@pytest.mark.parametrize("contract", ["legacy", "v1"])
@pytest.mark.parametrize("advance,first_hour,first_minute,final_minute,lights_minute", [
    (15, 10, 45, 45, 35),
    (0, 11, 0, 0, 50),
])
def test_fallback_normal_window_publication_and_daemon_restart(
    tmp_path: Path, contract: str, advance: int, first_hour: int, first_minute: int,
    final_minute: int, lights_minute: int,
) -> None:
    """Catch fallback being published as a plain alarm or offset again by consumers."""
    cfg = _config(tmp_path, schedule_contract=contract, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, deadline_lead_minutes=0, prep_advance_minutes=advance,
    ))
    now = _nyc(2026, 10, 15, 5)
    ha = _FakeHA()
    with patch("commutecompass.ha_client.call_service", side_effect=(
        ha.call_service if contract == "v1" else lambda *args, **kwargs: True
    )) as service, patch("commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch):
        result, ok = publish(cfg, now=now, plans_for_day=_provider({}))
    assert ok and result.source == "fallback"
    first = _nyc(2026, 10, 15, first_hour, first_minute)
    final = _nyc(2026, 10, 15, 11 if advance else 12, final_minute)
    lights = _nyc(2026, 10, 15, 10, lights_minute)
    assert result.wake_at == first
    assert result.deadline_at == final
    assert result.next_prep_at is None  # Fallback does not invent a calendar nap cutoff.
    if contract == "v1":
        raw = ha.calls[0][1]["data"]["snapshot"]
        schedule = parse_schedule(raw, first_lead_seconds=3600)
        assert schedule.kind == "commute"
        assert schedule.wake_at == first and schedule.deadline_at == final
        states = {
            cfg.alarm.contract_entity: {"state": "v1"},
            cfg.alarm.schedule_entity: {"state": raw},
        }
    else:
        payload = service.call_args.kwargs["data"]
        assert datetime.fromisoformat(payload["wake_at"]) == first
        assert datetime.fromisoformat(payload["deadline_at"]) == final
        states = {
            cfg.alarm.wake_utc_entity: {"state": payload["wake_at"]},
            cfg.alarm.window.deadline_utc_entity: {"state": payload["deadline_at"]},
        }
    feed = [inputs_from_states(states, cfg.alarm)]
    assert feed[0].wake_at == first and feed[0].deadline_at == final
    # HA's established lights rule is D - first lead - ten minutes.
    assert feed[0].deadline_at - timedelta(minutes=70) == lights
    settings = Settings.from_config(cfg.alarm)
    assert settings.window is not None and settings.window.deadline_lead == timedelta(0)
    clock = [first - timedelta(seconds=1)]
    cache = AlarmCache(tmp_path / "fallback-daemon.json")
    player = ProfilePlayer()
    daemon = AlarmDaemon(reader=lambda: feed[0], player=player, cache=cache,
                         settings=settings, clock=lambda: clock[0])
    daemon.run_once()
    clock[0] = first
    daemon.run_once()
    assert daemon.state.phase == "window" and player.profiles == [None]
    clock[0] = final - timedelta(minutes=1)
    feed[0] = replace(feed[0], snooze_pressed_at=clock[0])
    daemon.run_once()
    assert daemon.state.snooze_until == final
    # Persisted fallback geometry must survive an offline restart unchanged.
    clock[0] = final
    offline = ProfilePlayer()
    restarted = AlarmDaemon(reader=lambda: Inputs(ha_ok=False), player=offline, cache=cache,
                            settings=settings, clock=lambda: clock[0])
    restarted.run_once()
    assert restarted.state.deadline == final
    assert restarted.state.phase == "final" and restarted.state.mode == "ringing"
    assert offline.profiles == [None]
    clock[0] = final + timedelta(minutes=1)
    restarted.reader = lambda: replace(feed[0], snooze_pressed_at=clock[0],
                                       stop_pressed_at=clock[0])
    restarted.run_once()
    assert restarted.state.phase == "final" and restarted.state.mode == "ringing"
    assert offline.events == ["start"]
    restarted.activity()
    restarted.run_once()
    assert str(restarted.state.mode) == "idle" and offline.events == ["start", "stop"]


@pytest.mark.parametrize("leave_hour,leave_minute,expected_first,expected_final,source", [
    (11, 50, (10, 35), (11, 35), "calendar"),
    (12, 10, (10, 45), (11, 45), "fallback"),
    (12, 0, (10, 45), (11, 45), None),
])
def test_calendar_fallback_selection_compares_effective_finals(
    tmp_path: Path, leave_hour: int, leave_minute: int,
    expected_first: tuple[int, int], expected_final: tuple[int, int], source: str | None,
) -> None:
    """A later calendar first ring must not beat the fallback's earlier final."""
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    leave = _nyc(2026, 10, 15, leave_hour, leave_minute)
    plan = _commute_plan(leave + timedelta(minutes=13))
    original = plan.model_dump()
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]}))
    assert result.wake_at == _nyc(2026, 10, 15, *expected_first)
    assert result.deadline_at == _nyc(2026, 10, 15, *expected_final)
    if source is not None:
        assert result.source == source
    assert result.next_prep_at == leave - timedelta(minutes=60)
    assert plan.model_dump() == original


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("with_calendar", [False, True])
def test_manual_later_than_fallback_conflicts_only_with_real_calendar(
    tmp_path: Path, force: bool, with_calendar: bool,
) -> None:
    cfg = _config(tmp_path, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    # Fallback wins normal selection, but must never become a manual conflict.
    plans = [_commute_plan(_nyc(2026, 10, 15, 13, 13))] if with_calendar else []
    manual = ManualWake(_nyc(2026, 10, 15, 14), force)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): plans}), manual)
    calendar_wins = with_calendar and not force
    assert result.source == ("calendar" if calendar_wins else "manual")
    assert result.deadline_at == (_nyc(2026, 10, 15, 12, 45) if calendar_wins
                                  else manual.deadline_at)
    assert result.wake_at == (_nyc(2026, 10, 15, 11, 45) if calendar_wins
                              else _nyc(2026, 10, 15, 13))
    assert bool(result.warning and "conflicts" in result.warning) == with_calendar


@pytest.mark.parametrize("contract", ["legacy", "v1"])
def test_retained_fallback_effective_deadline_not_advanced_twice(
    tmp_path: Path, contract: str,
) -> None:
    cfg = _config(tmp_path, schedule_contract=contract, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    ha = _FakeHA()
    now = _nyc(2026, 10, 15, 5)
    with patch("commutecompass.ha_client.call_service", side_effect=(
        ha.call_service if contract == "v1" else lambda *args, **kwargs: True
    )), patch("commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch):
        first, ok = publish(cfg, now=now, plans_for_day=_provider({}))
    assert ok

    def broken(day: datetime) -> list[Plan]:
        raise RuntimeError("offline fixture")

    with patch("commutecompass.alarm.fetch_entity_state", return_value=None):
        retained = compute_wake(cfg, now, broken)
    assert retained.source == "retained"
    assert retained.wake_at == first.wake_at == _nyc(2026, 10, 15, 10, 45)
    assert retained.deadline_at == first.deadline_at == _nyc(2026, 10, 15, 11, 45)


@pytest.mark.parametrize("contract", ["legacy", "v1"])
def test_degraded_recovery_fallback_opens_normal_window(tmp_path: Path, contract: str) -> None:
    cfg = _config(tmp_path, schedule_contract=contract, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    ha = _FakeHA()

    def broken(day: datetime) -> list[Plan]:
        raise RuntimeError("offline fixture")

    with patch("commutecompass.ha_client.call_service", side_effect=(
        ha.call_service if contract == "v1" else lambda *args, **kwargs: True
    )) as service, patch("commutecompass.alarm.fetch_entity_state", side_effect=lambda *a, **kw: (
        ha.helper if ha.calls else None
    )), patch("commutecompass.notify.build_notifier"):
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=broken)
    assert ok and result.source == "fallback" and result.warning
    assert result.day == date(2026, 10, 16)
    assert result.wake_at == _nyc(2026, 10, 16, 10, 45)
    assert result.deadline_at == _nyc(2026, 10, 16, 11, 45)
    if contract == "v1":
        schedule = parse_schedule(ha.calls[0][1]["data"]["snapshot"], first_lead_seconds=3600)
        assert schedule.kind == "commute" and schedule.deadline_at == result.deadline_at
    else:
        assert datetime.fromisoformat(service.call_args.kwargs["data"]["deadline_at"]) == result.deadline_at


@pytest.mark.parametrize("contract", ["legacy", "v1"])
def test_fallback_selection_preserves_calendar_nap_cutoff(
    tmp_path: Path, contract: str,
) -> None:
    cfg = _config(tmp_path, schedule_contract=contract, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    plan = _commute_plan(_nyc(2026, 10, 15, 15, 13))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]}))
    assert result.source == "fallback"
    assert result.wake_at == _nyc(2026, 10, 15, 10, 45)
    assert result.next_prep_at == _nyc(2026, 10, 15, 14)
    if contract == "v1":
        raw = serialize_schedule(build_schedule(result, cfg, issued_at=_nyc(2026, 10, 15, 5)),
                                 first_lead_seconds=3600)
        states = {cfg.alarm.contract_entity: {"state": "v1"},
                  cfg.alarm.schedule_entity: {"state": raw}}
    else:
        payload = build_payload(result, window=True)
        wake_at = payload["wake_at"]
        next_prep_at = payload["next_prep_at"]
        deadline_at = payload["deadline_at"]
        assert isinstance(wake_at, str)
        assert isinstance(next_prep_at, str)
        assert isinstance(deadline_at, str)
        states = {cfg.alarm.wake_utc_entity: {"state": wake_at},
                  cfg.alarm.next_prep_utc_entity: {"state": next_prep_at},
                  cfg.alarm.window.deadline_utc_entity: {"state": deadline_at}}
    nap = _nyc(2026, 10, 15, 13, 55)
    states[cfg.alarm.nap_utc_entity] = {"state": nap.isoformat()}
    inputs = inputs_from_states(states, cfg.alarm)
    assert inputs.prep_threshold == _nyc(2026, 10, 15, 14)
    settings = Settings.from_config(cfg.alarm)
    state, _ = step(DaemonState(), nap - timedelta(seconds=1), inputs, settings)
    state, _ = step(state, nap, inputs, settings)
    assert state.active_kind == "nap" and state.phase == ""
    pressed = _nyc(2026, 10, 15, 13, 56)
    state, _ = step(state, pressed, replace(inputs, snooze_pressed_at=pressed), settings)
    assert state.snooze_until == _nyc(2026, 10, 15, 14)
    state, actions = step(state, _nyc(2026, 10, 15, 14), inputs, settings)
    assert state.phase == "" and state.mode == "ringing"
    assert any(isinstance(a, StartRing) for a in actions)


@pytest.mark.parametrize("month,day,fallback", [(3, 8, "03:10"), (11, 1, "02:10")])
def test_fallback_window_crosses_dst_in_absolute_minutes(
    tmp_path: Path, month: int, day: int, fallback: str,
) -> None:
    cfg = _config(tmp_path, fallback_time=fallback, window=AlarmWindowConfig(
        first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    now = _nyc(2026, month, day, 12) - timedelta(days=1)
    result = compute_wake(cfg, now, _provider({}))
    assert result.source == "fallback" and result.day == date(2026, month, day)
    assert result.wake_at.astimezone(UTC) == datetime(2026, month, day, 5, 55, tzinfo=UTC)
    assert result.deadline_at is not None
    assert result.deadline_at.astimezone(UTC) == datetime(2026, month, day, 6, 55, tzinfo=UTC)


def test_disabled_fallback_remains_plain_alarm_with_shared_advance(tmp_path: Path) -> None:
    cfg = _config(tmp_path, fallback_time="12:00", window=AlarmWindowConfig(
        enabled=False, first_alarm_lead_minutes=60, prep_advance_minutes=15,
    ))
    with patch("commutecompass.ha_client.call_service", return_value=True) as service:
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_provider({}))
    assert ok and result.source == "fallback"
    assert result.wake_at == _nyc(2026, 10, 15, 12)
    assert result.deadline_at is None
    assert "deadline_at" not in service.call_args.kwargs["data"]
