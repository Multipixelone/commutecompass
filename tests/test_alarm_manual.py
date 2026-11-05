"""Manual ready-by deadline selection and publication."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_alarm import _commute_plan, _config, _nyc, _plan, _provider

from commutecompass.alarm import build_payload, build_schedule, compute_wake, publish
from commutecompass.alarm_state import ManualWake, clear_manual, load_manual, save_manual
from commutecompass.models import Plan


def test_manual_exact_deadline_and_l70_l60_l_timeline(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    deadline = _nyc(2026, 10, 15, 9)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5), _provider({}), ManualWake(deadline))
    assert result.source == "manual"
    assert result.deadline_at == deadline
    assert result.wake_at == _nyc(2026, 10, 15, 8)
    payload = build_payload(result, window=True)
    assert payload["deadline_at"] == deadline.isoformat()
    # HA lights precede the first alarm by ten minutes; final remains exact L.
    lights = result.deadline_at.astimezone(UTC) - timedelta(minutes=70)
    assert lights == _nyc(2026, 10, 15, 7, 50).astimezone(UTC)
    schedule = build_schedule(result, cfg, issued_at=_nyc(2026, 10, 15, 5))
    assert schedule.kind == "commute"
    assert schedule.deadline_at == deadline.astimezone(UTC)


def test_next_day_0230_manual_has_exact_0130_wake(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    deadline = _nyc(2026, 10, 16, 2, 30)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 20), _provider({}), ManualWake(deadline))
    assert result.source == "manual"
    assert result.day == date(2026, 10, 16)
    assert result.wake_at.astimezone(UTC) == deadline.astimezone(UTC) - timedelta(hours=1)
    assert result.wake_at == _nyc(2026, 10, 16, 1, 30)


def test_manual_replaces_fallback_even_after_noon(tmp_path: Path) -> None:
    result = compute_wake(_config(tmp_path), _nyc(2026, 10, 15, 5), _provider({}),
                          ManualWake(_nyc(2026, 10, 15, 16)))
    assert result.wake_at == _nyc(2026, 10, 15, 15, 23)
    assert result.source == "manual"


@pytest.mark.parametrize("force", [False, True])
def test_earlier_calendar_deadline_wins_unless_forced(tmp_path: Path, force: bool) -> None:
    cfg = _config(tmp_path)
    plan = _commute_plan(_nyc(2026, 10, 15, 9))
    manual = ManualWake(_nyc(2026, 10, 15, 10), force)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]}), manual)
    assert result.deadline_at == (manual.deadline_at if force else plan.leave_at)
    assert result.source == ("manual" if force else "calendar")
    assert result.manual_deadline_at == manual.deadline_at
    assert result.warning and "conflicts" in result.warning


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_prep_only_alarm_cannot_mask_earlier_commute_deadline(
    tmp_path: Path, force: bool, reverse: bool,
) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    virtual = _plan(_nyc(2026, 10, 15, 6, 30), "Virtual preparation")
    virtual.event.virtual = True
    commute = _commute_plan(_nyc(2026, 10, 15, 8, 13))
    assert commute.leave_at == _nyc(2026, 10, 15, 8)
    plans = [virtual, commute]
    if reverse:
        plans.reverse()
    manual = ManualWake(_nyc(2026, 10, 15, 10), force)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): plans}), manual)
    assert result.source == ("manual" if force else "calendar")
    assert result.deadline_at == (manual.deadline_at if force else commute.leave_at)
    assert result.wake_at == _nyc(2026, 10, 15, 9 if force else 7)
    assert result.warning and "conflicts" in result.warning
    assert result.manual_deadline_at == manual.deadline_at


def test_afternoon_calendar_earlier_than_manual_still_competes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plan = _commute_plan(_nyc(2026, 10, 15, 15))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]}),
                          ManualWake(_nyc(2026, 10, 15, 16)))
    assert result.source == "calendar"
    assert result.deadline_at == plan.leave_at


@pytest.mark.parametrize("has_calendar", [False, True])
def test_future_manual_does_not_skip_earlier_day(tmp_path: Path, has_calendar: bool) -> None:
    cfg = _config(tmp_path)
    plan = _commute_plan(_nyc(2026, 10, 15, 9))
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5),
                          _provider({date(2026, 10, 15): [plan]} if has_calendar else {}),
                          ManualWake(_nyc(2026, 10, 16, 10)))
    assert result.day == date(2026, 10, 15)
    assert result.source == ("calendar" if has_calendar else "fallback")
    assert result.manual_deadline_at is None


def test_manual_active_window_survives_republish_then_expires(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manual = ManualWake(_nyc(2026, 10, 15, 9))
    save_manual(cfg, manual)
    with patch("commutecompass.ha_client.call_service", return_value=True):
        first, _ = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_provider({}))
        active, ok = publish(cfg, now=_nyc(2026, 10, 15, 8, 45), plans_for_day=_provider({}))
        expired, _ = publish(cfg, now=_nyc(2026, 10, 15, 9), plans_for_day=_provider({}))
    assert ok
    assert active.source == "manual" and active.wake_at == first.wake_at
    assert expired.source == "fallback"
    assert load_manual(cfg, _nyc(2026, 10, 15, 9)) is None


@pytest.mark.parametrize("month,day,hour,fold", [(3, 8, 3, 0), (11, 1, 1, 1)])
def test_manual_lead_uses_absolute_utc_across_dst(tmp_path: Path, month: int, day: int,
                                               hour: int, fold: int) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    deadline = _nyc(2026, month, day, hour, 30).replace(fold=fold)
    now = (deadline.astimezone(UTC) - timedelta(hours=5)).astimezone(deadline.tzinfo)
    result = compute_wake(cfg, now, _provider({}), ManualWake(deadline))
    assert result.source == "manual"
    assert result.deadline_at == deadline
    assert result.wake_at.astimezone(UTC) == deadline.astimezone(UTC) - timedelta(hours=1)


def test_planner_exception_does_not_hide_manual(tmp_path: Path) -> None:
    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("unavailable")
    result = compute_wake(_config(tmp_path), _nyc(2026, 10, 15, 5), failed,
                          ManualWake(_nyc(2026, 10, 15, 9)))
    assert result.source == "manual"
    assert result.warning and "planning failed" in result.warning


@pytest.mark.parametrize("force", [False, True])
def test_degraded_planning_retains_earlier_same_day_calendar_unless_forced(
    tmp_path: Path, force: bool,
) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    calendar = _commute_plan(_nyc(2026, 11, 5, 9))
    with patch("commutecompass.ha_client.call_service", return_value=True):
        previous, _ = publish(cfg, now=_nyc(2026, 11, 4, 20),
                              plans_for_day=_provider({date(2026, 11, 5): [calendar]}))
    save_manual(cfg, ManualWake(_nyc(2026, 11, 5, 10), force))

    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("unavailable")

    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.notify.build_notifier",
    ):
        result, ok = publish(cfg, now=_nyc(2026, 11, 4, 21), plans_for_day=failed)
    assert ok
    assert result.source == ("manual" if force else "retained")
    assert result.wake_at == (_nyc(2026, 11, 5, 9) if force else previous.wake_at)
    assert result.warning


def test_degraded_planning_retains_today_before_tomorrow_forced_manual(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    today = _commute_plan(_nyc(2026, 11, 4, 11))
    with patch("commutecompass.ha_client.call_service", return_value=True):
        previous, _ = publish(cfg, now=_nyc(2026, 11, 4, 5),
                              plans_for_day=_provider({date(2026, 11, 4): [today]}))
    save_manual(cfg, ManualWake(_nyc(2026, 11, 5, 9), force=True))

    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("unavailable")

    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.notify.build_notifier",
    ):
        result, ok = publish(cfg, now=_nyc(2026, 11, 4, 6), plans_for_day=failed)
    assert ok and result.source == "retained"
    assert result.wake_at == previous.wake_at


def test_early_computation_failure_still_selects_upcoming_manual(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    manual = ManualWake(_nyc(2026, 11, 5, 9))
    with patch("commutecompass.alarm._compute_wake", side_effect=RuntimeError("early failure")):
        result = compute_wake(cfg, _nyc(2026, 11, 4, 20), _provider({}), manual)
    assert result.source == "manual"
    assert result.deadline_at == manual.deadline_at
    assert result.wake_at.astimezone(UTC) == _nyc(2026, 11, 5, 8).astimezone(UTC)
    assert result.warning and "computation failed" in result.warning


def test_all_provider_days_fail_but_next_day_manual_still_publishes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    manual = ManualWake(_nyc(2026, 11, 5, 9))
    save_manual(cfg, manual)

    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("all calendar snapshots unavailable")

    with patch("commutecompass.ha_client.call_service", return_value=True) as service, patch(
        "commutecompass.notify.build_notifier",
    ) as notifier:
        result, ok = publish(cfg, now=_nyc(2026, 11, 4, 20), plans_for_day=failed)
    assert ok and result.source == "manual"
    assert result.deadline_at == manual.deadline_at
    assert result.wake_at.astimezone(UTC) == _nyc(2026, 11, 5, 8).astimezone(UTC)
    assert result.warning and "planning failed" in result.warning
    assert service.call_args.kwargs["data"]["deadline_at"] == manual.deadline_at.isoformat()
    notifier.return_value.send.assert_called_once()


def test_dry_run_leaves_manual_and_database_unchanged(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_manual(cfg, ManualWake(_nyc(2026, 10, 15, 9)))
    database = Path(cfg.paths.db_path)
    before = database.read_bytes()
    with patch("commutecompass.alarm_state.bind_manual") as bind, patch(
        "commutecompass.alarm_state.save_last_publish",
    ) as save, patch("commutecompass.ha_client.call_service") as service:
        result, ok = publish(cfg, dry_run=True, now=_nyc(2026, 10, 15, 8, 45),
                             plans_for_day=_provider({}))
    assert not ok and result.source == "manual"
    assert database.read_bytes() == before
    bind.assert_not_called()
    save.assert_not_called()
    service.assert_not_called()


def test_publish_cannot_bind_replaced_manual_even_if_deadline_identical(tmp_path: Path) -> None:
    from commutecompass.alarm_state import clear_manual_for_wake

    cfg = _config(tmp_path)
    manual = ManualWake(_nyc(2026, 10, 16, 9))
    save_manual(cfg, manual)

    def replace_during_publish(*args: object, **kwargs: object) -> bool:
        save_manual(cfg, manual)  # New durable identity with the same value.
        return True

    with patch("commutecompass.ha_client.call_service", side_effect=replace_during_publish):
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 20), plans_for_day=_provider({}))
    assert ok
    assert not clear_manual_for_wake(cfg, result.wake_at)
    assert load_manual(cfg, _nyc(2026, 10, 15, 20)) == manual


def test_manual_wake_can_cross_deadline_logical_day_boundary(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 120
    manual = ManualWake(_nyc(2026, 10, 16, 3, 30))
    save_manual(cfg, manual)
    with patch("commutecompass.ha_client.call_service", return_value=True):
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 20), plans_for_day=_provider({}))
    assert ok and result.source == "manual"
    assert result.day == date(2026, 10, 16)
    assert result.wake_at == _nyc(2026, 10, 16, 1, 30)
    # A cross-boundary binding must still be consumable by the selected wake.
    from commutecompass.alarm_state import clear_manual_for_wake

    assert clear_manual_for_wake(cfg, result.wake_at)
    assert load_manual(cfg, _nyc(2026, 10, 15, 20)) is None


@pytest.mark.parametrize("clear", [False, True])
def test_calendar_winner_tied_to_manual_not_retained_after_clear_or_expiry(
    tmp_path: Path, clear: bool,
) -> None:
    cfg = _config(tmp_path)
    save_manual(cfg, ManualWake(_nyc(2026, 10, 16, 10)))
    plan = _commute_plan(_nyc(2026, 10, 16, 9))
    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.notify.build_notifier",
    ):
        published, _ = publish(cfg, now=_nyc(2026, 10, 15, 20),
                               plans_for_day=_provider({date(2026, 10, 16): [plan]}))
    assert published.source == "calendar"
    assert published.manual_deadline_at == _nyc(2026, 10, 16, 10)
    if clear:
        clear_manual(cfg)
    now = _nyc(2026, 10, 15, 21) if clear else _nyc(2026, 10, 16, 10)

    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("unavailable")

    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.notify.build_notifier",
    ):
        result, ok = publish(cfg, now=now, plans_for_day=failed)
    assert ok and result.source == "fallback"
    assert result.wake_at == (_nyc(2026, 10, 16, 12) if clear else _nyc(2026, 10, 17, 12))


def test_cleared_manual_publication_is_not_retained(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_manual(cfg, ManualWake(_nyc(2026, 10, 16, 9)))
    with patch("commutecompass.ha_client.call_service", return_value=True):
        publish(cfg, now=_nyc(2026, 10, 15, 20), plans_for_day=_provider({}))
    clear_manual(cfg)
    def failed(day: datetime) -> list[Plan]:
        raise RuntimeError("unavailable")
    result = compute_wake(cfg, _nyc(2026, 10, 15, 21), failed)
    assert result.source == "fallback"
    assert result.wake_at == _nyc(2026, 10, 16, 12)
