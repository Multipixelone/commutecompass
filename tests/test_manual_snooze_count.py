"""Per-manual-alarm budgets, using fake clocks/HA and the existing alarm fixtures.

The older manual/window suites cover omitted-count behavior, ordinary calendar
windows, fallback and naps. These regressions target explicit policy crossing
persistence, publication and daemon boundaries; no live HA or audio is used.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from test_alarm import _commute_plan, _config, _nyc, _provider
from test_alarm_daemon import FakePlayer, kinds

from commutecompass import alarm as alarm_mod
from commutecompass import cli, timeutil
from commutecompass.alarm import build_payload, build_schedule, compute_wake, publish
from commutecompass.alarm_contract import (
    MAX_SNAPSHOT_LENGTH,
    ScheduleError,
    new_schedule,
    parse_schedule,
    serialize_schedule,
)
from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    DaemonState,
    Inputs,
    Settings,
    StartRing,
    WindowSettings,
    inputs_from_states,
    step,
)
from commutecompass.alarm_state import ManualWake, load_manual, save_manual
from commutecompass.config import AlarmConfig, Config

DEADLINE = _nyc(2026, 10, 15, 10)
FIRST = _nyc(2026, 10, 15, 9, 40).replace(second=30)
ISSUED = _nyc(2026, 10, 15, 5)
SETTINGS = Settings(window=WindowSettings(first_lead=timedelta(hours=1)))


def manual_inputs(count: int = 3, *, wake: datetime = FIRST,
                  deadline: datetime = DEADLINE, **kwargs: Any) -> Inputs:
    return Inputs(ha_ok=True, next_alarm=wake, kind="wake", wake_at=wake,
                  wake_known=True, deadline_at=deadline, deadline_known=True,
                  manual_snoozes=count, **kwargs)


def first_ring(count: int = 3, *, wake: datetime = FIRST) -> DaemonState:
    state, actions = step(DaemonState(), wake, manual_inputs(count, wake=wake), SETTINGS)
    assert state.mode == "ringing" and any(isinstance(a, StartRing) for a in actions)
    return state


@pytest.mark.parametrize("count,expected", [
    (0, DEADLINE),
    (1, _nyc(2026, 10, 15, 9, 53).replace(second=30)),
    (3, FIRST),
])
def test_explicit_count_sets_exact_first_ring_not_global_lead(
    tmp_path: Path, count: int, expected: datetime,
) -> None:
    # Catches treating zero as omission, rounded minutes, or a global-lead override.
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    cfg.alarm.window.calendar_advance_minutes = 15
    cfg.alarm.window.deadline_lead_minutes = 5
    cfg.alarm.window.snooze_minutes = 9
    before = cfg.alarm.model_dump()
    result = compute_wake(cfg, ISSUED, _provider({}),
                          ManualWake(DEADLINE, manual_snoozes=count))
    assert result.source == "manual"
    assert result.wake_at == expected and result.deadline_at == DEADLINE
    assert result.manual_snoozes == count
    schedule = build_schedule(result, cfg, issued_at=ISSUED)
    assert schedule.kind == "manual" and schedule.manual_snoozes == count
    assert parse_schedule(serialize_schedule(schedule)) == schedule
    assert build_payload(result, window=True)["manual_snoozes"] == count
    assert cfg.alarm.model_dump() == before


def test_omission_keeps_old_positional_force_and_current_settings(tmp_path: Path) -> None:
    # Existing positional ManualWake(deadline, force) must not become a count.
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    manual = ManualWake(DEADLINE, True)
    assert manual.force is True and manual.manual_snoozes is None
    result = compute_wake(cfg, ISSUED, _provider({}), manual)
    assert result.wake_at == _nyc(2026, 10, 15, 9)
    assert result.manual_snoozes is None
    schedule = build_schedule(result, cfg, issued_at=ISSUED)
    assert schedule.kind == "commute"
    assert '"snoozes"' not in serialize_schedule(schedule, first_lead_seconds=3600)


@pytest.mark.parametrize("force", [False, True])
def test_explicit_budget_belongs_only_to_manual_winner(tmp_path: Path, force: bool) -> None:
    # Calendar conflict selection already has coverage, but not policy leakage.
    cfg = _config(tmp_path)
    cfg.alarm.window.first_alarm_lead_minutes = 60
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 13))  # leaves at 09:00
    result = compute_wake(cfg, ISSUED, _provider({date(2026, 10, 15): [plan]}),
                          ManualWake(DEADLINE, force, manual_snoozes=3))
    assert result.warning and "conflicts" in result.warning
    assert result.manual_deadline_at == DEADLINE
    assert result.source == ("manual" if force else "calendar")
    assert result.wake_at == (FIRST if force else _nyc(2026, 10, 15, 8))
    assert result.manual_snoozes == (3 if force else None)
    schedule = build_schedule(result, cfg, issued_at=ISSUED)
    assert schedule.kind == ("manual" if force else "commute")
    assert ('"snoozes"' in serialize_schedule(schedule, first_lead_seconds=3600)) is force
    assert build_payload(result, window=True)["manual_snoozes"] == (3 if force else "")


@pytest.mark.parametrize("deadline,now,expected_utc", [
    (_nyc(2026, 10, 16, 0, 10), _nyc(2026, 10, 15, 20), "2026-10-16T03:50:30+00:00"),
    (_nyc(2026, 3, 8, 3, 10), _nyc(2026, 3, 7, 20), "2026-03-08T06:50:30+00:00"),
    (_nyc(2026, 11, 1, 1, 10).replace(fold=1), _nyc(2026, 10, 31, 20),
     "2026-11-01T05:50:30+00:00"),
])
def test_count_timeline_crosses_midnight_and_dst_as_absolute_seconds(
    tmp_path: Path, deadline: datetime, now: datetime, expected_utc: str,
) -> None:
    # Wall-clock subtraction can lose an hour or choose the wrong fold.
    result = compute_wake(_config(tmp_path), now, _provider({}),
                          ManualWake(deadline, manual_snoozes=3))
    assert result.source == "manual"
    assert result.deadline_at is not None
    assert result.deadline_at.astimezone(UTC) == deadline.astimezone(UTC)
    assert result.wake_at.astimezone(UTC).isoformat() == expected_utc
    state, _ = step(DaemonState(), result.wake_at,
                    manual_inputs(wake=result.wake_at, deadline=deadline), SETTINGS)
    assert state.phase == "window" and state.deadline is not None
    assert state.deadline.astimezone(UTC) == deadline.astimezone(UTC)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_manual_budget_roundtrips_and_republish_does_not_move_first_ring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    # A reload/republication must not drop zero or recompute from remaining time.
    cfg = _config(tmp_path)
    manual = ManualWake(DEADLINE, manual_snoozes=count)
    save_manual(cfg, manual)
    assert load_manual(cfg.model_copy(), ISSUED) == manual
    service = Mock(return_value=True)
    monkeypatch.setattr("commutecompass.ha_client.call_service", service)
    first, ok = publish(cfg, now=ISSUED, plans_for_day=_provider({}))
    again, ok_again = publish(cfg.model_copy(), now=_nyc(2026, 10, 15, 9, 59),
                             plans_for_day=_provider({}))
    assert ok and ok_again
    assert again.source == "manual" and again.wake_at == first.wake_at
    assert again.deadline_at == DEADLINE and again.manual_snoozes == count
    assert service.call_args.kwargs["data"]["manual_snoozes"] == count


@pytest.mark.parametrize("bad", [-1, 1.5, 13, True, False])
def test_invalid_manual_count_is_rejected_before_replacing_saved_intent(
    tmp_path: Path, bad: object,
) -> None:
    # bool is an int subclass; neither it nor fractions may become a real budget.
    cfg = _config(tmp_path)
    saved = ManualWake(DEADLINE, manual_snoozes=3)
    save_manual(cfg, saved)
    with pytest.raises(ValueError):
        save_manual(cfg, ManualWake(DEADLINE, manual_snoozes=bad))  # type: ignore[arg-type]
    assert load_manual(cfg, ISSUED) == saved


@pytest.mark.parametrize("bad", [-1, 1.5, 13, "true"])
def test_invalid_persisted_budget_is_not_reinterpreted_as_valid(
    tmp_path: Path, bad: object,
) -> None:
    # Corrupt durable state must fail closed, not truncate or drop to old defaults.
    cfg = _config(tmp_path)
    save_manual(cfg, ManualWake(DEADLINE, manual_snoozes=3))
    with sqlite3.connect(cfg.paths.db_path) as connection:
        connection.execute("UPDATE alarm_state SET manual_snoozes = ? WHERE id = 1", (bad,))
    try:
        loaded = load_manual(cfg, ISSUED)
    except ValueError:
        return  # Either an explicit rejection or no usable intent is safe.
    assert loaded is None


def test_old_manual_database_without_count_remains_readable(tmp_path: Path) -> None:
    # Additive migration must preserve an already-scheduled manual deadline.
    cfg = _config(tmp_path)
    with sqlite3.connect(cfg.paths.db_path) as connection:
        connection.executescript("""
            CREATE TABLE alarm_state (
                id INTEGER PRIMARY KEY, manual_deadline TEXT,
                manual_force INTEGER NOT NULL DEFAULT 0,
                manual_version INTEGER NOT NULL DEFAULT 0,
                bound_wake TEXT, bound_deadline TEXT, bound_version INTEGER,
                last_publish_json TEXT
            );
        """)
        connection.execute(
            "INSERT INTO alarm_state(id, manual_deadline, manual_force) VALUES(1, ?, 1)",
            (DEADLINE.astimezone(UTC).isoformat(),),
        )
    manual = load_manual(cfg, ISSUED)
    assert manual == ManualWake(DEADLINE, True)
    assert manual.manual_snoozes is None


def manual_wire() -> str:
    return (
        '{"v":1,"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"issued_at":"2026-10-15T09:00:00Z","kind":"manual",'
        '"wake_at":"2026-10-15T13:40:30Z","deadline_at":"2026-10-15T14:00:00Z",'
        '"next_prep_at":null,"snoozes":3}'
    )


def test_manual_wire_is_canonical_and_independent_of_global_lead() -> None:
    # Existing exact-string v1 tests cover old kinds, not this extension's key order.
    schedule = new_schedule(kind="manual", issued_at=ISSUED, wake_at=FIRST,
                            deadline_at=DEADLINE, next_prep_at=None,
                            manual_snoozes=3, schedule_id="a" * 32)
    raw = serialize_schedule(schedule, first_lead_seconds=7200)
    assert raw == manual_wire()
    assert len(raw) <= MAX_SNAPSHOT_LENGTH
    assert parse_schedule(raw, first_lead_seconds=1800) == schedule


@pytest.mark.parametrize("count,wake", [
    (0, DEADLINE), (3, FIRST), (12, _nyc(2026, 10, 15, 8, 42)),
])
@pytest.mark.parametrize("with_prep,expected_length", [(False, 221), (True, 246)])
def test_manual_wire_microsecond_boundaries_fit_ha_helper(
    count: int, wake: datetime, with_prep: bool, expected_length: int,
) -> None:
    # The extra field plus four full-precision timestamps must fit input_text.
    schedule = new_schedule(
        kind="manual", issued_at=ISSUED.replace(microsecond=123456),
        wake_at=wake.replace(microsecond=123456),
        deadline_at=DEADLINE.replace(microsecond=123456),
        next_prep_at=ISSUED.replace(microsecond=123456) if with_prep else None,
        manual_snoozes=count, schedule_id="a" * 32,
    )
    raw = serialize_schedule(schedule)
    assert len(raw) == expected_length + (1 if count == 12 else 0)
    assert len(raw) <= 247 <= MAX_SNAPSHOT_LENGTH == 255
    assert parse_schedule(raw) == schedule


@pytest.mark.parametrize("bad", [-1, 1.5, 13, True, False, "3", None])
def test_manual_wire_rejects_invalid_count_without_coercion(bad: object) -> None:
    body = json.loads(manual_wire())
    body["snoozes"] = bad
    with pytest.raises(ScheduleError):
        parse_schedule(json.dumps(body, separators=(",", ":")))


@pytest.mark.parametrize("change", ["missing_count", "wrong_timing", "commute_with_count"])
def test_manual_wire_requires_count_and_matching_pair_only_for_manual(change: str) -> None:
    body = json.loads(manual_wire())
    if change == "missing_count":
        del body["snoozes"]
    elif change == "wrong_timing":
        body["wake_at"] = "2026-10-15T13:40:00Z"
    else:
        body["kind"] = "commute"
        body["wake_at"] = "2026-10-15T13:00:00Z"
    with pytest.raises(ScheduleError):
        parse_schedule(json.dumps(body, separators=(",", ":")), first_lead_seconds=3600)


@pytest.mark.parametrize("kind", ["commute", "legacy", "none"])
def test_pre_extension_v1_has_no_snooze_key_and_still_parses(kind: str) -> None:
    schedule = new_schedule(
        kind=kind, issued_at=ISSUED,  # type: ignore[arg-type]
        wake_at=None if kind == "none" else _nyc(2026, 10, 15, 9),
        deadline_at=DEADLINE if kind == "commute" else None,
        next_prep_at=None, first_lead_seconds=3600,
    )
    raw = serialize_schedule(schedule, first_lead_seconds=3600)
    assert '"snoozes"' not in raw
    assert parse_schedule(raw, first_lead_seconds=3600) == schedule
    assert schedule.manual_snoozes is None


def legacy_states(cfg: AlarmConfig) -> dict[str, Any]:
    return {
        cfg.wake_utc_entity: {"state": FIRST.astimezone(UTC).isoformat()},
        cfg.window.deadline_utc_entity: {"state": DEADLINE.astimezone(UTC).isoformat()},
        cfg.window.manual_policy_entity: {"state": json.dumps({
            "wake_at": FIRST.astimezone(UTC).isoformat(),
            "deadline_at": DEADLINE.astimezone(UTC).isoformat(), "snoozes": 3,
        })},
    }


def test_legacy_and_v1_inputs_preserve_manual_policy_without_global_lead_match() -> None:
    cfg = AlarmConfig()
    assert cfg.window.manual_policy_entity == "input_text.commute_manual_wake_policy"
    legacy = inputs_from_states(legacy_states(cfg), cfg)
    v1_cfg = AlarmConfig(schedule_contract="v1")
    v1 = inputs_from_states({
        v1_cfg.contract_entity: {"state": "v1"},
        v1_cfg.schedule_entity: {"state": manual_wire()},
        # A stale legacy policy cannot override authoritative v1.
        cfg.window.manual_policy_entity: {"state": '{"snoozes":0}'},
    }, v1_cfg)
    for inputs in (legacy, v1):
        assert inputs.manual_snoozes == 3
        assert inputs.wake_at == FIRST and inputs.deadline_at == DEADLINE
        state, _ = step(DaemonState(), FIRST, inputs, SETTINGS)
        assert state.phase == "window" and state.manual_snoozes == 3


@pytest.mark.parametrize("field,value", [
    ("wake_at", "2026-10-15T13:40:31+00:00"),
    ("deadline_at", "2026-10-15T14:00:01+00:00"),
    ("snoozes", -1), ("snoozes", 1.5), ("snoozes", 13),
    ("snoozes", True), ("snoozes", False),
])
def test_stale_or_invalid_legacy_helper_does_not_attach_budget_to_another_pair(
    field: str, value: object,
) -> None:
    # Independent legacy helper writes may briefly describe different schedules.
    cfg = AlarmConfig()
    states = legacy_states(cfg)
    policy = json.loads(states[cfg.window.manual_policy_entity]["state"])
    policy[field] = value
    states[cfg.window.manual_policy_entity]["state"] = json.dumps(policy)
    inputs = inputs_from_states(states, cfg)
    assert inputs.manual_snoozes is None


@pytest.mark.parametrize("policy_state", [None, "broken", '{"snoozes":3}'])
def test_unknown_legacy_policy_does_not_globalize_cached_pending_manual(
    policy_state: str | None,
) -> None:
    # A split helper refresh/outage must not discard the cached explicit budget.
    cfg = AlarmConfig()
    settings = Settings.from_config(cfg)
    states = legacy_states(cfg)
    valid = inputs_from_states(states, cfg)
    pending, _ = step(DaemonState(), ISSUED, valid, settings)
    if policy_state is None:
        del states[cfg.window.manual_policy_entity]
    else:
        states[cfg.window.manual_policy_entity] = {"state": policy_state}
    unknown = inputs_from_states(states, cfg)
    assert unknown.manual_snoozes is None and not unknown.manual_policy_known
    state, _ = step(pending, FIRST, unknown, settings)
    assert state.mode == "ringing" and state.phase == "window"
    assert state.manual_snoozes == 3 and state.deadline == DEADLINE


@pytest.fixture
def cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "_load_config", lambda _path: cfg)
    monkeypatch.setattr(timeutil, "now_nyc", lambda: ISSUED)
    return cfg


@pytest.fixture
def local_publish(cli_config: Config, monkeypatch: pytest.MonkeyPatch) -> Mock:
    # Exercise actual intent persistence/computation and CLI formatting without HA.
    def local(cfg: Config, **_kwargs: object) -> tuple[Any, bool]:
        return compute_wake(cfg, timeutil.now_nyc(), _provider({}),
                            load_manual(cfg, timeutil.now_nyc())), True

    publisher = Mock(side_effect=local)
    monkeypatch.setattr(alarm_mod, "publish", publisher)
    return publisher


def test_cli_confirmation_and_status_keep_half_minute_precision_and_count(
    cli_config: Config, local_publish: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--leave", "10am", "--snoozes", "3"])
    assert result.exit_code == 0, result.output
    assert "09:40:30" in result.output and "10:00" in result.output
    assert "3" in result.output and "snooz" in result.output.lower()
    saved = load_manual(cli_config, ISSUED)
    assert saved is not None and saved.manual_snoozes == 3
    states = {
        **legacy_states(cli_config.alarm),
        cli_config.alarm.enabled_entity: {"state": "on"},
        cli_config.alarm.next_alarm_entity: {
            "state": FIRST.isoformat(), "attributes": {"kind": "wake"},
        },
    }
    monkeypatch.setattr("commutecompass.alarm_daemon.fetch_states", lambda *_args: states)
    status = CliRunner().invoke(cli.cli, ["alarm", "status", "--json"])
    assert status.exit_code == 0, status.output
    manual = json.loads(status.output)["manual"]
    assert manual["manual_snoozes"] == 3
    assert datetime.fromisoformat(manual["wake_at"]) == FIRST
    assert manual["selected"] is True
    human = CliRunner().invoke(cli.cli, ["alarm", "status"])
    assert human.exit_code == 0, human.output
    assert "09:40:30" in human.output and "3" in human.output


def test_status_matching_sensor_without_verified_policy_does_not_claim_manual_selected(
    cli_config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Coincident next_alarm timestamps cannot prove the explicit budget reached HA.
    save_manual(cli_config, ManualWake(DEADLINE, manual_snoozes=3))
    states = {
        **legacy_states(cli_config.alarm),
        cli_config.alarm.enabled_entity: {"state": "on"},
        cli_config.alarm.next_alarm_entity: {
            "state": FIRST.isoformat(), "attributes": {"kind": "wake"},
        },
    }
    del states[cli_config.alarm.window.manual_policy_entity]
    monkeypatch.setattr("commutecompass.alarm_daemon.fetch_states", lambda *_args: states)
    result = CliRunner().invoke(cli.cli, ["alarm", "status", "--json"])
    assert result.exit_code == 0, result.output
    manual = json.loads(result.output)["manual"]
    assert manual["manual_snoozes"] == 3
    assert datetime.fromisoformat(manual["wake_at"]) == FIRST
    assert manual["selected"] is False


@pytest.mark.parametrize("count,expected", [
    (0, DEADLINE),
    (1, _nyc(2026, 10, 15, 9, 53).replace(second=30)),
    (12, _nyc(2026, 10, 15, 8, 42)),
])
def test_cli_accepts_zero_one_and_upper_bound_without_changing_global_policy(
    cli_config: Config, local_publish: Mock, count: int, expected: datetime,
) -> None:
    before = cli_config.alarm.model_dump()
    result = CliRunner().invoke(cli.cli, [
        "alarm", "wake", "--leave", "10am", "--snoozes", str(count),
    ])
    assert result.exit_code == 0, result.output
    assert expected.isoformat() in result.output
    saved = load_manual(cli_config, ISSUED)
    assert saved is not None and saved.manual_snoozes == count
    assert cli_config.alarm.model_dump() == before


def test_first_ring_exactly_now_is_not_late(
    cli_config: Config, local_publish: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timeutil, "now_nyc", lambda: FIRST)
    result = CliRunner().invoke(cli.cli, [
        "alarm", "wake", "--leave", "10am", "--snoozes", "3",
    ])
    assert result.exit_code == 0, result.output
    assert "09:40:30" in result.output
    local_publish.assert_called_once()


@pytest.mark.parametrize("force", [False, True])
def test_late_first_ring_requires_allow_late_even_with_force(
    cli_config: Config, local_publish: Mock, monkeypatch: pytest.MonkeyPatch, force: bool,
) -> None:
    monkeypatch.setattr(timeutil, "now_nyc", lambda: _nyc(2026, 10, 15, 9, 50))
    args = ["alarm", "wake", "--leave", "10am", "--snoozes", "3"]
    if force:
        args += ["--force"]
    rejected = CliRunner().invoke(cli.cli, args)
    assert rejected.exit_code == cli.EXIT_USAGE, rejected.output
    assert "--allow-late" in rejected.output
    local_publish.assert_not_called()
    assert load_manual(cli_config, ISSUED, dry_run=True) is None
    accepted = CliRunner().invoke(cli.cli, [*args, "--allow-late"])
    assert accepted.exit_code == 0, accepted.output
    manual = load_manual(cli_config, ISSUED)
    assert manual is not None
    assert manual.manual_snoozes == 3 and manual.force is force
    assert "09:40:30" in accepted.output  # original time, not now minus count


@pytest.mark.parametrize("extra,exit_code,error", [
    (["--snoozes", "-1"], 2, "0<=x<=12"),
    (["--snoozes", "1.5"], 2, "integer"),
    (["--snoozes", "13"], 2, "0<=x<=12"),
    (["--snoozes", "true"], 2, "integer"),
    (["--allow-late"], cli.EXIT_USAGE, "--snoozes"),
])
def test_cli_rejects_invalid_count_or_allow_late_without_count_before_persistence(
    cli_config: Config, local_publish: Mock, extra: list[str], exit_code: int, error: str,
) -> None:
    # Click parsing uses exit 2; option-combination checks use the runtime usage code.
    saved = ManualWake(DEADLINE, manual_snoozes=1)
    save_manual(cli_config, saved)
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--leave", "10am", *extra])
    assert result.exit_code == exit_code, result.output
    assert "Error:" in result.output and error in result.output
    if exit_code == 2:
        assert "Invalid value for '--snoozes'" in result.output
    assert load_manual(cli_config, ISSUED) == saved
    local_publish.assert_not_called()


@pytest.mark.parametrize("extra", [["--snoozes", "0"], ["--allow-late"]])
def test_clear_cannot_accept_manual_policy_options(
    cli_config: Config, local_publish: Mock, extra: list[str],
) -> None:
    saved = ManualWake(DEADLINE, manual_snoozes=3)
    save_manual(cli_config, saved)
    result = CliRunner().invoke(cli.cli, ["alarm", "wake", "--clear", *extra])
    assert result.exit_code == cli.EXIT_USAGE
    assert load_manual(cli_config, ISSUED) == saved
    local_publish.assert_not_called()


def test_zero_goes_straight_to_exact_final_ignoring_prep_and_deadline_lead() -> None:
    # Zero must not fall through to the old lead/default budget or an early final.
    assert SETTINGS.window is not None
    settings = replace(SETTINGS, window=replace(SETTINGS.window, deadline_lead=timedelta(minutes=5)))
    inputs = manual_inputs(0, wake=DEADLINE, prep_known=True,
                           prep_threshold=_nyc(2026, 10, 15, 9))
    pending, actions = step(DaemonState(), _nyc(2026, 10, 15, 9, 59), inputs, settings)
    assert actions == [] and pending.mode == "idle"
    assert pending.pending_manual_snoozes == 0
    state, _ = step(pending, DEADLINE, inputs, settings)
    assert state.mode == "ringing" and state.phase == "final"
    assert state.manual_snoozes == 0 and state.snoozes == 0
    for now in (DEADLINE + timedelta(seconds=1), DEADLINE + timedelta(hours=1)):
        state, actions = step(state, now, replace(inputs, snooze_pressed_at=now,
                                                stop_pressed_at=now), settings)
        assert actions == [] and state.mode == "ringing" and state.snoozes == 0
    state, actions = step(state, DEADLINE + timedelta(hours=1),
                          replace(inputs, activity_at=DEADLINE + timedelta(hours=1)), settings)
    assert state.mode == "idle" and "stop:activity" in kinds(actions)


@pytest.mark.parametrize("control", ["snooze_pressed_at", "stop_pressed_at"])
def test_three_accepted_actions_use_exact_intervals_then_final(control: str) -> None:
    state = first_ring()
    for pressed, expected in [
        (_nyc(2026, 10, 15, 9, 40).replace(second=31),
         _nyc(2026, 10, 15, 9, 47).replace(second=1)),
        (_nyc(2026, 10, 15, 9, 47).replace(second=2),
         _nyc(2026, 10, 15, 9, 53).replace(second=32)),
        (_nyc(2026, 10, 15, 9, 53).replace(second=33), DEADLINE),
    ]:
        previous = state.snoozes
        inputs = (manual_inputs(stop_pressed_at=pressed) if control == "stop_pressed_at"
                  else manual_inputs(snooze_pressed_at=pressed))
        state, actions = step(state, pressed, inputs, SETTINGS)
        assert kinds(actions) == ["stop:snooze", "state:snoozed"]
        assert state.snooze_until == expected and state.snoozes == previous + 1
        state, _ = step(state, expected, manual_inputs(), SETTINGS)
        assert state.mode == "ringing"
    assert state.phase == "final" and state.snoozes == 3
    late = DEADLINE + timedelta(seconds=1)
    inputs = (manual_inputs(stop_pressed_at=late) if control == "stop_pressed_at"
              else manual_inputs(snooze_pressed_at=late))
    state, actions = step(state, late, inputs, SETTINGS)
    assert actions == [] and state.mode == "ringing" and state.snoozes == 3


@pytest.mark.parametrize("control", ["snooze_pressed_at", "stop_pressed_at"])
def test_exhausted_budget_cannot_silence_pre_final_ring(control: str) -> None:
    # Budget exhaustion matters before final too, not only after deadline checks.
    state = replace(first_ring(), snoozes=3)
    pressed = FIRST + timedelta(seconds=1)
    inputs = (manual_inputs(stop_pressed_at=pressed) if control == "stop_pressed_at"
              else manual_inputs(snooze_pressed_at=pressed))
    state, actions = step(state, pressed, inputs, SETTINGS)
    assert state.mode == "ringing" and state.phase == "window"
    assert state.snoozes == 3 and state.snooze_until is None and actions == []
    state, actions = step(state, DEADLINE, manual_inputs(), SETTINGS)
    assert state.mode == "ringing" and state.phase == "final"
    assert state.snoozes == 3 and actions == []


def test_one_snooze_and_simultaneous_final_controls_cannot_silence_final() -> None:
    # Deadline wins even when an otherwise fresh stop/snooze arrives on that tick.
    wake = _nyc(2026, 10, 15, 9, 53).replace(second=30)
    state = first_ring(1, wake=wake)
    pressed = wake + timedelta(seconds=1)
    inputs = manual_inputs(1, wake=wake)
    state, _ = step(state, pressed, replace(inputs, snooze_pressed_at=pressed), SETTINGS)
    assert state.snoozes == 1 and state.snooze_until == DEADLINE
    state, actions = step(state, DEADLINE, replace(
        inputs, snooze_pressed_at=DEADLINE, stop_pressed_at=DEADLINE,
    ), SETTINGS)
    assert state.mode == "ringing" and state.phase == "final"
    assert state.snoozes == 1 and "stop:snooze" not in kinds(actions)
    later = DEADLINE + timedelta(seconds=1)
    state, actions = step(state, later, replace(inputs, stop_pressed_at=later), SETTINGS)
    assert state.mode == "ringing" and actions == []


def test_only_accepted_new_actions_count_not_stale_duplicate_or_auto_timeout() -> None:
    state = first_ring()
    pressed = FIRST + timedelta(seconds=1)
    state, actions = step(state, pressed, manual_inputs(snooze_pressed_at=FIRST), SETTINGS)
    assert actions == [] and state.snoozes == 0
    state, _ = step(state, pressed, manual_inputs(snooze_pressed_at=pressed), SETTINGS)
    until = state.snooze_until
    assert until is not None
    state, _ = step(state, pressed + timedelta(seconds=1),
                    manual_inputs(snooze_pressed_at=pressed), SETTINGS)
    assert state.snoozes == 1 and state.snooze_until == until
    state, _ = step(state, until, manual_inputs(snooze_pressed_at=pressed), SETTINGS)
    state, actions = step(state, until + timedelta(seconds=1),
                          manual_inputs(snooze_pressed_at=pressed), SETTINGS)
    assert state.snoozes == 1 and state.mode == "ringing" and actions == []
    # With a long enough window, the 15-minute timeout is not a user action.
    long_wake = _nyc(2026, 10, 15, 8, 42)
    long_state = first_ring(12, wake=long_wake)
    long_state, _ = step(long_state, _nyc(2026, 10, 15, 8, 57),
                         manual_inputs(12, wake=long_wake), SETTINGS)
    assert long_state.snoozes == 0


def test_explicit_snooze_ignores_global_interval_and_prep_but_caps_at_exact_deadline() -> None:
    assert SETTINGS.window is not None
    settings = replace(SETTINGS, window=replace(
        SETTINGS.window, snooze=timedelta(minutes=9), deadline_lead=timedelta(minutes=5),
    ))
    state = first_ring()
    early = _nyc(2026, 10, 15, 9, 41)
    state, _ = step(state, early, manual_inputs(snooze_pressed_at=early), settings)
    assert state.snooze_until == _nyc(2026, 10, 15, 9, 47).replace(second=30)
    state, _ = step(state, state.snooze_until, manual_inputs(), settings)
    pressed = _nyc(2026, 10, 15, 9, 57)
    state, _ = step(state, pressed, manual_inputs(
        snooze_pressed_at=pressed, prep_known=True, prep_threshold=pressed,
    ), settings)
    assert state.mode == "snoozed" and state.snooze_until == DEADLINE
    state, _ = step(state, DEADLINE, manual_inputs(snooze_pressed_at=DEADLINE), settings)
    assert state.phase == "final" and state.mode == "ringing" and state.snoozes == 2


def test_restart_keeps_pending_and_consumed_budget_and_does_not_accept_republished_policy(
    tmp_path: Path,
) -> None:
    cache = AlarmCache(tmp_path / "manual-cache.json")
    pending, _ = step(DaemonState(), ISSUED, manual_inputs(), SETTINGS)
    cache.save(pending)
    assert cache.load().pending_manual_snoozes == 3
    state = first_ring()
    pressed = FIRST + timedelta(seconds=1)
    state, _ = step(state, pressed, manual_inputs(snooze_pressed_at=pressed), SETTINGS)
    cache.save(state)
    restored = cache.load()
    assert restored.manual_snoozes == 3 and restored.snoozes == 1
    assert restored.snooze_until == _nyc(2026, 10, 15, 9, 47).replace(second=1)
    player = FakePlayer()
    clock = [restored.snooze_until]
    daemon = AlarmDaemon(lambda: Inputs(ha_ok=False), player, cache, SETTINGS,
                         clock=lambda: clock[0])
    daemon.run_once()
    assert daemon.state.mode == "ringing" and daemon.state.snoozes == 1
    clock[0] += timedelta(seconds=1)
    # A republication's replacement policy cannot overwrite an active budget.
    daemon.reader = lambda: manual_inputs(0)
    daemon.run_once()
    assert daemon.state.manual_snoozes == 3 and daemon.state.snoozes == 1
    daemon.reader = lambda: manual_inputs(snooze_pressed_at=clock[0])
    daemon.run_once()
    assert daemon.state.snoozes == 2
    assert daemon.state.snooze_until == _nyc(2026, 10, 15, 9, 53).replace(second=32)


@pytest.mark.parametrize("count,wake,phase", [(0, DEADLINE, "final"), (3, FIRST, "window")])
def test_offline_restart_promotes_pending_policy_at_its_original_instant(
    tmp_path: Path, count: int, wake: datetime, phase: str,
) -> None:
    # Cache-only startup must carry the policy along with the pending deadline.
    cache = AlarmCache(tmp_path / "pending.json")
    pending, _ = step(DaemonState(), ISSUED, manual_inputs(count, wake=wake), SETTINGS)
    cache.save(pending)
    player = FakePlayer()
    daemon = AlarmDaemon(lambda: Inputs(ha_ok=False), player, cache, SETTINGS, clock=lambda: wake)
    daemon.run_once()
    assert daemon.state.mode == "ringing" and daemon.state.phase == phase
    assert daemon.state.active == wake and daemon.state.deadline == DEADLINE
    assert daemon.state.manual_snoozes == count and daemon.state.snoozes == 0
    assert player.events == ["start"]


def test_unrelated_replan_does_not_transfer_budget_or_change_locked_manual_deadline() -> None:
    state = first_ring()
    unrelated = Inputs(ha_ok=True, wake_known=True, deadline_known=True,
                       wake_at=_nyc(2026, 10, 15, 8), deadline_at=_nyc(2026, 10, 15, 9))
    state, _ = step(state, FIRST + timedelta(seconds=1), unrelated, SETTINGS)
    assert state.deadline == DEADLINE and state.manual_snoozes == 3
    # Conversely a manual publication cannot convert an active calendar window.
    ordinary = Inputs(ha_ok=True, wake_known=True, deadline_known=True,
                      wake_at=_nyc(2026, 10, 15, 9), deadline_at=DEADLINE)
    calendar, _ = step(DaemonState(), _nyc(2026, 10, 15, 9), ordinary, SETTINGS)
    calendar, _ = step(calendar, FIRST, manual_inputs(), SETTINGS)
    assert calendar.manual_snoozes is None


@pytest.mark.parametrize("bad", [-1, 1.5, 13, True, False])
def test_corrupt_cache_cannot_coerce_an_invalid_budget(tmp_path: Path, bad: object) -> None:
    cache = AlarmCache(tmp_path / "cache.json")
    cache.save(first_ring())
    body = json.loads(cache.path.read_text())
    body["manual_snoozes"] = bad
    cache.path.write_text(json.dumps(body))
    try:
        restored = cache.load()
    except ValueError:
        return
    assert restored.manual_snoozes is None


def test_old_window_cache_without_manual_fields_keeps_unlimited_policy(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "old-cache.json")
    cache.path.write_text(json.dumps({
        "mode": "ringing", "active": _nyc(2026, 10, 15, 9).isoformat(),
        "active_kind": "wake", "ring_started": _nyc(2026, 10, 15, 9).isoformat(),
        "phase": "window", "deadline": DEADLINE.isoformat(), "snoozes": 4,
    }))
    restored = cache.load()
    assert restored.manual_snoozes is None and restored.pending_manual_snoozes is None
    now = _nyc(2026, 10, 15, 9, 1)
    state, actions = step(restored, now, Inputs(ha_ok=False, snooze_pressed_at=now), SETTINGS)
    assert state.snoozes == 5 and kinds(actions) == ["stop:snooze", "state:snoozed"]
