"""Manual wake controls: fake persistence, HA, clock and audio (no live I/O)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from test_alarm import _config
from test_alarm_daemon import FakePlayer
from test_alarm_window import WAKE, D, W, m, ring_first, win

from commutecompass import alarm as alarm_mod
from commutecompass import alarm_state, cli, timeutil
from commutecompass.alarm_daemon import (
    AlarmDaemon,
    DaemonState,
    Inputs,
    _retire_manual_control,
)
from commutecompass.alarm_state import ManualWake
from commutecompass.config import Config


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    config = _config(tmp_path, window={"enabled": True})
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    monkeypatch.setattr(timeutil, "now_nyc", lambda: m(-120))
    return config


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Mock:
    publish = Mock(return_value=(SimpleNamespace(
        wake_at=WAKE, next_prep_at=None, source="manual", warning=None,
    ), True))
    monkeypatch.setattr(alarm_mod, "publish", publish)
    return publish


@pytest.mark.parametrize("value", ["09:00", "9am", "9:30am"])
def test_wake_persists_then_publishes(
    cfg: Config, published: Mock, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    calls: list[object] = []
    parse = Mock(return_value=D)
    monkeypatch.setattr(alarm_state, "parse_manual_deadline", parse)
    monkeypatch.setattr(alarm_state, "save_manual", lambda _cfg, value: calls.append(value))
    def record_publish(*_args: object, **_kw: object) -> tuple[SimpleNamespace, bool]:
        calls.append("publish")
        return SimpleNamespace(
            wake_at=WAKE, next_prep_at=None, source="manual", warning=None,
        ), True

    published.side_effect = record_publish
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--leave", value, "--force"])
    assert result.exit_code == 0, result.output
    assert calls == [ManualWake(D, True), "publish"]
    parse.assert_called_once_with(value, None, m(-120))
    assert "(manual)" in result.output


def test_wake_explicit_date(cfg: Config, published: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    parse = Mock(return_value=D)
    monkeypatch.setattr(alarm_state, "parse_manual_deadline", parse)
    monkeypatch.setattr(alarm_state, "save_manual", Mock())
    result = CliRunner().invoke(cli.cli, [
        "alarm", "wake", "--leave", "9am", "--date", "2026-10-15",
    ])
    assert result.exit_code == 0, result.output
    parse.assert_called_once_with("9am", date(2026, 10, 15), m(-120))


@pytest.mark.parametrize("args", [
    [], ["--force"], ["--date", "2026-10-15"],
    ["--clear", "--leave", "9am"], ["--clear", "--force"],
    ["--clear", "--date", "2026-10-15"],
    ["--leave", "9am", "--date", "2026-02-30"],
    ["--leave", "9am", "--date", "20261015"],
])
def test_invalid_wake_usage(cfg: Config, published: Mock, args: list[str]) -> None:
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", *args])
    assert result.exit_code == cli.EXIT_USAGE, result.output
    published.assert_not_called()


@pytest.mark.parametrize("reason", ["past deadline", "invalid time"])
def test_parser_errors_are_usage(
    cfg: Config, published: Mock, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    monkeypatch.setattr(alarm_state, "parse_manual_deadline", Mock(side_effect=ValueError(reason)))
    save = Mock()
    monkeypatch.setattr(alarm_state, "save_manual", save)
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--leave", "9am"])
    assert result.exit_code == cli.EXIT_USAGE
    assert reason in result.output
    save.assert_not_called()
    published.assert_not_called()


def test_disabled_window_rejected(cfg: Config, published: Mock) -> None:
    cfg.alarm.window.enabled = False
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--clear"])
    assert result.exit_code == cli.EXIT_USAGE
    published.assert_not_called()


def test_clear_is_local_and_republishes(
    cfg: Config, published: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual", clear)
    published.return_value = (SimpleNamespace(
        wake_at=WAKE, next_prep_at=None, source="calendar", warning="calendar conflict",
    ), False)
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--clear"])
    clear.assert_called_once_with(cfg)
    published.assert_called_once_with(cfg, dry_run=False)
    assert result.exit_code == cli.EXIT_TRANSIENT
    assert "calendar conflict" in result.output


@pytest.mark.parametrize("control", ["off", "skip"])
@pytest.mark.parametrize("success", [True, False])
def test_cli_controls_only_clear_after_ha_success(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, control: str, success: bool,
) -> None:
    from commutecompass import ha_client

    clear = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual", clear)
    monkeypatch.setattr(ha_client, "call_service", Mock(return_value=success))
    result = CliRunner().invoke(cli.cli, ["alarm", control])
    assert result.exit_code == (0 if success else cli.EXIT_TRANSIENT), result.output
    assert clear.call_count == int(success)


@pytest.mark.parametrize("enabled,offset,kind,selected,conflict", [
    ("on", 0, "wake", True, False),
    ("on", -15, "wake", False, True),
    ("off", 0, "wake", False, False),
    ("unknown", 0, "wake", False, False),
    ("on", 0, "nap", False, False),
])
def test_status_reports_pending_without_claiming_selection(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, enabled: str, offset: int,
    kind: str, selected: bool, conflict: bool,
) -> None:
    from commutecompass import alarm_daemon

    load = Mock(return_value=ManualWake(D, True))
    monkeypatch.setattr(alarm_state, "load_manual", load)
    monkeypatch.setattr(alarm_daemon, "fetch_states", lambda *_args: {
        cfg.alarm.enabled_entity: {"state": enabled},
        cfg.alarm.next_alarm_entity: {
            "state": (WAKE + timedelta(minutes=offset)).isoformat(), "attributes": {"kind": kind},
        },
    })
    result = CliRunner().invoke(cli.cli, ["alarm", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enabled"] is (True if enabled == "on" else False if enabled == "off" else None)
    manual = payload["manual"]
    assert manual["selected"] is selected
    assert manual["conflict"] is conflict
    assert manual["force"] is True
    load.assert_called_once_with(cfg, m(-120), dry_run=True)
    human = CliRunner().invoke(cli.cli, ["alarm", "status"])
    assert "Pending manual:" in human.output
    assert ("(manual, leave 08:00)" in human.output) is selected
    if enabled == "off":
        assert "(disabled)" in human.output
    elif enabled == "unknown":
        assert "Alarm enabled: unknown" in human.output
        assert "(switch unknown)" in human.output


@pytest.mark.parametrize("control", ["desktop", "off", "skip", "clear"])
def test_daemon_consumes_manual_before_deadline_without_resurrection(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, control: str,
) -> None:
    pending: list[ManualWake] = [ManualWake(D)]
    bound = WAKE

    def clear_bound(_cfg: Config, wake: object) -> bool:
        if wake == bound and pending:
            pending.clear()
            return True
        return False

    monkeypatch.setattr(alarm_state, "clear_manual_for_wake", clear_bound)
    monkeypatch.setattr(alarm_state, "clear_manual", lambda _cfg: pending.clear())
    now = m(-30)
    inputs = {
        "desktop": win(activity_at=now), "off": win(enabled=False),
        "skip": win(skipped_at=WAKE), "clear": win(wake=None, deadline=None),
    }[control]
    cache = Mock()
    cache.load.return_value = ring_first()
    daemon = AlarmDaemon(
        reader=lambda: inputs, player=FakePlayer(), cache=cache, settings=W,
        clock=lambda: now, state=ring_first(),
        retire_manual=lambda i, b, a, n: _retire_manual_control(cfg, i, b, a, n),
    )
    daemon.run_once()
    assert now < D
    assert not pending
    assert daemon.state.mode == "idle"
    # A subsequent publisher computation cannot resurrect deleted intent before L.
    result = alarm_mod.compute_wake(cfg, m(-29), lambda _day: [],
                                    manual=pending[0] if pending else None)
    assert result.source != "manual"
    assert result.wake_at != WAKE
    daemon.run_once()
    assert daemon.state.mode == "idle"


def test_master_off_retires_unbound_future_manual(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual", clear)
    _retire_manual_control(cfg, Inputs(ha_ok=True, enabled=False),
                           DaemonState(), DaemonState(), m(-90))
    clear.assert_called_once_with(cfg)


@pytest.mark.parametrize("inputs", [
    Inputs(ha_ok=False), Inputs(ha_ok=True),
    win(wake=WAKE + timedelta(minutes=10), deadline=D + timedelta(minutes=10)),
])
def test_unknown_and_replacement_preserve_manual(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, inputs: Inputs,
) -> None:
    clear = Mock()
    bound = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual", clear)
    monkeypatch.setattr(alarm_state, "clear_manual_for_wake", bound)
    before = ring_first()
    from commutecompass.alarm_daemon import step

    after, _ = step(before, m(-30), inputs, W)
    _retire_manual_control(cfg, inputs, before, after, m(-30))
    clear.assert_not_called()
    bound.assert_not_called()


def test_stale_completion_compares_original_wake(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from commutecompass.alarm_daemon import step

    replacement = ManualWake(D + timedelta(hours=1))
    pending = [replacement]
    attempts: list[object] = []

    def compare(_cfg: Config, wake: object) -> bool:
        attempts.append(wake)
        if wake == replacement.deadline_at - timedelta(minutes=37):
            pending.clear()
            return True
        return False

    monkeypatch.setattr(alarm_state, "clear_manual_for_wake", compare)
    before = ring_first()
    inputs = win(activity_at=m(-30))
    after, _ = step(before, m(-30), inputs, W)
    _retire_manual_control(cfg, inputs, before, after, m(-30))
    assert attempts == [WAKE]
    assert pending == [replacement]


def test_latched_off_during_ha_outage_does_not_clear_replacement(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from commutecompass.alarm_daemon import step

    clear = Mock()
    bound = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual", clear)
    monkeypatch.setattr(alarm_state, "clear_manual_for_wake", bound)
    before = replace(ring_first(), enabled=False)
    inputs = Inputs(ha_ok=False)
    after, _ = step(before, m(-30), inputs, W)
    _retire_manual_control(cfg, inputs, before, after, m(-30))
    clear.assert_not_called()
    bound.assert_not_called()


def test_matching_skip_and_clear_pending_wake(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = Mock()
    monkeypatch.setattr(alarm_state, "clear_manual_for_wake", bound)
    before = DaemonState(pending=WAKE, pending_kind="wake")
    _retire_manual_control(cfg, win(skipped_at=WAKE), before, DaemonState(), m(-45))
    bound.assert_called_once_with(cfg, WAKE)
    bound.reset_mock()
    _retire_manual_control(cfg, win(wake=None), before, DaemonState(), m(-45))
    bound.assert_called_once_with(cfg, WAKE)


def test_retirement_failure_does_not_block_audio_restore() -> None:
    player = FakePlayer()
    cache = Mock()
    cache.load.return_value = ring_first()
    daemon = AlarmDaemon(
        reader=lambda: win(activity_at=m(-30)), player=player, cache=cache,
        settings=W, state=ring_first(), clock=lambda: m(-30),
        retire_manual=Mock(side_effect=OSError("unavailable")),
    )
    daemon.run_once()
    assert daemon.state.mode == "idle"
    assert "stop" in player.events
