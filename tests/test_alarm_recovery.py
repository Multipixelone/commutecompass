"""Safety recovery and cross-logical-day anchor regression coverage."""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, patch

import pytest
from test_alarm import _config, _nyc, _plan, _provider

from commutecompass.alarm import compute_wake, publish, wake_for_day
from commutecompass.alarm_state import save_last_publish
from commutecompass.config import Config
from commutecompass.models import Plan
from commutecompass.notify import STDOUT_MSG_END, STDOUT_MSG_START
from commutecompass.timeutil import logical_day_bounds_nyc


@pytest.fixture(autouse=True)
def _no_live_ha_reads() -> Iterator[None]:
    with patch("commutecompass.alarm.fetch_entity_state", return_value=None):
        yield


def _broken(day: datetime) -> list[Plan]:
    raise RuntimeError("planner unavailable")


@pytest.mark.parametrize("error", [None, "too_imminent", "no_route"])
def test_overlapping_started_event_cannot_poison_three_future_days(tmp_path: Path, error: str | None) -> None:
    cfg = _config(tmp_path)
    stale = _plan(_nyc(2026, 10, 12, 7), error=error)
    # An overlap provider returns the same older event on every requested day.
    result = compute_wake(cfg, _nyc(2026, 10, 15, 20), lambda day: [stale])
    assert result.wake_at == _nyc(2026, 10, 16, 12)
    assert result.warning is None
    assert result.next_prep_at is None


def test_multiday_overlap_does_not_reanchor_on_each_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plan = _plan(_nyc(2026, 10, 12, 7), error="no_route")
    plan.event.end = _nyc(2026, 10, 20, 18)
    today, _ = logical_day_bounds_nyc(_nyc(2026, 10, 15, 10))
    assert wake_for_day(today, [plan], cfg, now=_nyc(2026, 10, 15, 10)) == (
        _nyc(2026, 10, 15, 12), None,
    )


def test_midnight_overlap_owned_by_start_logical_day(tmp_path: Path) -> None:
    # All-day events are excluded by CalendarClient before this layer. A
    # midnight-shaped imported/cache event still must not anchor the next day.
    cfg = _config(tmp_path)
    plan = _plan(_nyc(2026, 10, 15, 0))
    plan.event.start = _nyc(2026, 10, 15, 0)
    plan.event.end = _nyc(2026, 10, 16, 0)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5), lambda day: [plan])
    assert result.wake_at == _nyc(2026, 10, 15, 12)
    assert result.warning is None


def test_start_membership_not_prep_membership(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plan = _plan(_nyc(2026, 10, 16, 1, 30))
    plan.event.start = _nyc(2026, 10, 16, 2, 30)
    result = compute_wake(cfg, _nyc(2026, 10, 15, 23), lambda day: [plan])
    assert result.wake_at == plan.prep_at
    assert result.day == date(2026, 10, 16)
    assert result.warning is None


@pytest.mark.parametrize("day", [(2026, 3, 8), (2026, 11, 1)])
def test_day_filter_across_dst(tmp_path: Path, day: tuple[int, int, int]) -> None:
    cfg = _config(tmp_path)
    now = _nyc(*day, 10)
    stale = _plan((now.astimezone(UTC) - timedelta(days=2)).astimezone(now.tzinfo))
    result = compute_wake(cfg, now, lambda start: [stale])
    assert result.wake_at == _nyc(*day, 12)
    assert result.warning is None


def test_planner_error_uses_next_day_not_today_fallback(tmp_path: Path) -> None:
    result = compute_wake(_config(tmp_path), _nyc(2026, 10, 15, 5), _broken)
    assert result.wake_at == _nyc(2026, 10, 16, 12)
    assert result.source == "fallback"
    assert result.warning and "planning failed" in result.warning


def test_retains_future_success_on_error_and_persists_only_success(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    when = _nyc(2026, 10, 16, 8)
    with patch("commutecompass.ha_client.call_service", return_value=True):
        first, ok = publish(cfg, now=_nyc(2026, 10, 15, 20),
                            plans_for_day=_provider({date(2026, 10, 16): [_plan(when)]}))
    assert ok
    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.notify.build_notifier",
    ) as notifier:
        retained, ok = publish(cfg, now=_nyc(2026, 10, 15, 21), plans_for_day=_broken)
    assert ok
    assert retained.wake_at == first.wake_at
    assert retained.source == "retained"
    assert retained.warning and "retaining" in retained.warning
    notifier.return_value.send.assert_called_once()


def test_failed_publication_cannot_replace_last_success(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    original = _nyc(2026, 10, 16, 8)
    with patch("commutecompass.ha_client.call_service", return_value=True):
        publish(cfg, now=_nyc(2026, 10, 15, 20),
                plans_for_day=_provider({date(2026, 10, 16): [_plan(original)]}))
    with patch("commutecompass.ha_client.call_service", return_value=False):
        _, ok = publish(cfg, now=_nyc(2026, 10, 15, 21),
                        plans_for_day=_provider({date(2026, 10, 16): [_plan(_nyc(2026, 10, 16, 9))]}))
    assert not ok
    result = compute_wake(cfg, _nyc(2026, 10, 15, 22), _broken)
    assert result.source == "retained"
    assert result.wake_at == original


@pytest.mark.parametrize("contract", ["legacy", "v1"])
def test_initial_upgrade_preserves_future_live_wake(tmp_path: Path, contract: Literal["legacy", "v1"]) -> None:
    from commutecompass.alarm_contract import new_schedule, serialize_schedule

    cfg = _config(tmp_path)
    cfg.alarm.schedule_contract = contract
    now = _nyc(2026, 10, 15, 5)
    wake = _nyc(2026, 10, 16, 8)
    raw = (serialize_schedule(new_schedule(issued_at=now, kind="legacy", wake_at=wake,
                                           deadline_at=None, next_prep_at=None))
           if contract == "v1" else wake.isoformat())
    with patch("commutecompass.alarm.fetch_entity_state", return_value=raw) as read, patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ), patch("commutecompass.alarm.publish_v1"), patch("commutecompass.notify.build_notifier"):
        result, ok = publish(cfg, now=now, plans_for_day=_broken)
    assert ok and result.source == "retained"
    assert result.wake_at.astimezone(UTC) == wake.astimezone(UTC)
    read.assert_called_once()


def test_existing_history_never_resurrects_live_manual_after_clear(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_last_publish(cfg, {
        "wake_at": _nyc(2026, 10, 16, 8).isoformat(), "day": "2026-10-16",
        "source": "manual", "manual_deadline_at": _nyc(2026, 10, 16, 9).isoformat(),
        "deadline_at": _nyc(2026, 10, 16, 9).isoformat(),
    })
    with patch("commutecompass.alarm.fetch_entity_state") as read, patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ), patch("commutecompass.notify.build_notifier"):
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 20), plans_for_day=_broken)
    assert ok and result.source == "fallback"
    read.assert_not_called()


def test_manual_state_load_failure_still_publishes_safety(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch("commutecompass.alarm_state.load_manual", side_effect=RuntimeError("bad DB")), patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ) as service, patch("commutecompass.notify.build_notifier"):
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_provider({}))
    assert ok and result.warning
    assert result.wake_at == _nyc(2026, 10, 16, 12)
    service.assert_called_once()


def test_expired_retention_not_used(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_last_publish(cfg, {"wake_at": _nyc(2026, 10, 15, 8).isoformat(),
                            "day": "2026-10-15", "source": "calendar"})
    result = compute_wake(cfg, _nyc(2026, 10, 15, 9), _broken)
    assert result.source == "fallback"
    assert result.wake_at == _nyc(2026, 10, 16, 12)


def test_operational_warning_is_delivered_after_ha_publish(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    order: list[str] = []
    notifier = Mock()
    notifier.send.side_effect = lambda body: order.append("warning")

    def send(*args: object, **kwargs: object) -> bool:
        order.append("publish")
        return True

    with patch("commutecompass.notify.build_notifier", return_value=notifier), patch(
        "commutecompass.ha_client.call_service", side_effect=send,
    ):
        _, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_broken)
    assert ok and order == ["publish", "warning"]


def test_warning_delivery_failure_never_blocks_publish(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _config(tmp_path)
    notifier = Mock()
    notifier.send.side_effect = RuntimeError("offline")
    with patch("commutecompass.notify.build_notifier", return_value=notifier), patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ) as service:
        result, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_broken)
    assert ok and result.warning
    service.assert_called_once()
    assert "ALARM SAFETY" in caplog.text


def test_dry_run_safety_does_not_create_database_or_send(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch("commutecompass.notify.build_notifier") as notifier, patch(
        "commutecompass.ha_client.call_service",
    ) as service:
        result, ok = publish(cfg, dry_run=True, now=_nyc(2026, 10, 15, 5), plans_for_day=_broken)
    assert not ok and result.warning
    assert not Path(cfg.paths.db_path).exists()
    notifier.assert_not_called()
    service.assert_not_called()


def test_warning_is_telegram_escaped(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch("commutecompass.notify.build_notifier") as notifier, patch(
        "commutecompass.ha_client.call_service", return_value=True,
    ):
        publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_broken)
    body = notifier.return_value.send.call_args.args[0]
    assert r"\(RuntimeError\)" in body


def test_warning_uses_stdout_framing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _config(tmp_path)
    cfg.notify.mode = "stdout"
    with patch("commutecompass.ha_client.call_service", return_value=True):
        _, ok = publish(cfg, now=_nyc(2026, 10, 15, 5), plans_for_day=_broken)
    output = capsys.readouterr().out
    assert ok
    assert STDOUT_MSG_START in output and STDOUT_MSG_END in output
    assert "Alarm warning" in output


def test_default_dry_run_isolates_provider_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    token = Path(cfg.paths.oauth_token_path)
    token.write_text("original")
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    # tempfile caches its default; reset only for this test to exercise TMPDIR.
    monkeypatch.setattr(tempfile, "tempdir", None)
    scratch_roots: set[Path] = set()

    def planning(config: Config, start: datetime, end: datetime) -> list[Plan]:
        assert config.paths.db_path != cfg.paths.db_path
        root = Path(config.paths.db_path).parent
        scratch_roots.add(root)
        assert root.parent == temp_parent
        assert root.stat().st_mode & 0o777 == 0o700
        Path(config.paths.db_path).write_bytes(b"scratch")
        Path(config.paths.oauth_token_path).write_text("refreshed")
        return []

    with patch(
        "commutecompass.alarm.TemporaryDirectory", wraps=tempfile.TemporaryDirectory,
    ) as scratch_factory, patch("commutecompass.jobs.tomorrow.plan_window", side_effect=planning):
        result, ok = publish(cfg, dry_run=True, now=_nyc(2026, 10, 15, 5))
    assert not ok and result.warning is None
    scratch_factory.assert_called_once()
    assert scratch_factory.call_args.kwargs.get("dir") is None
    assert scratch_roots and all(not root.exists() for root in scratch_roots)
    assert not Path(cfg.paths.db_path).exists()
    assert token.read_text() == "original"
