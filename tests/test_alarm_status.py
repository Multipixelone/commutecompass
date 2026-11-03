"""Advisory daemon status and isolated v1 schedule authority (fake HA/audio only)."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_alarm_daemon import T0, FakePlayer, at, inp
from test_alarm_window import WAKE, D, W, m, win

from commutecompass.alarm_contract import (
    ScheduleKind,
    new_schedule,
    serialize_schedule,
    status_observation_fresh,
)
from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    DaemonState,
    Inputs,
    PipeWirePlayer,
    Settings,
    build_daemon,
    inputs_from_states,
    status_snapshot,
    step,
)
from commutecompass.config import AlarmConfig, Config, HomeAssistantConfig


def snapshot(kind: ScheduleKind = "commute") -> str:
    return serialize_schedule(new_schedule(
        kind=kind, issued_at=m(-60), wake_at=None if kind == "none" else WAKE,
        deadline_at=D if kind == "commute" else None, next_prep_at=None,
    ))


def recorder[T](sink: list[T]) -> Callable[[T], bool]:
    """Typed fake export/heartbeat callback: record the value and report success."""
    def record(value: T) -> bool:
        sink.append(value)
        return True
    return record


def states(cfg: AlarmConfig, raw: str | None = None, selector: str = "v1") -> dict[str, Any]:
    return {
        cfg.schedule_entity: {"state": snapshot() if raw is None else raw},
        cfg.contract_entity: {"state": selector},
        cfg.wake_utc_entity: {"state": at(400).isoformat()},
        cfg.window.deadline_utc_entity: {"state": at(500).isoformat()},
        cfg.next_prep_utc_entity: {"state": at(300).isoformat()},
        cfg.next_alarm_entity: {"state": at(400).isoformat(), "attributes": {"kind": "wake"}},
        cfg.nap_utc_entity: {"state": at(10).isoformat()},
        cfg.stop_button: {"state": at(1).isoformat()},
        cfg.snooze_button: {"state": at(2).isoformat()},
        cfg.skipped_utc_entity: {"state": at(3).isoformat()},
        cfg.enabled_entity: {"state": "off"},
    }


def test_v1_uses_only_validated_snapshot_but_controls_are_independent() -> None:
    cfg = AlarmConfig(schedule_contract="v1")
    parsed = inputs_from_states(states(cfg), cfg)
    assert parsed.wake_at == WAKE and parsed.deadline_at == D
    assert parsed.prep_threshold is None and parsed.prep_known
    assert parsed.next_alarm is None
    assert parsed.nap_at == at(10) and parsed.nap_known
    assert parsed.stop_pressed_at == at(1) and parsed.snooze_pressed_at == at(2)
    assert parsed.skipped_at == at(3) and parsed.enabled is False


@pytest.mark.parametrize("raw,selector", [("broken", "v1"), ("", "v1"), (None, "legacy"),
                                          (None, "unknown"), ('{"v":1}', "v1")])
def test_untrusted_v1_preserves_cached_and_active_lock(raw: str | None, selector: str) -> None:
    cfg = AlarmConfig(schedule_contract="v1")
    entities = states(cfg, raw, selector)
    entities[cfg.enabled_entity] = {"state": "on"}
    entities.pop(cfg.skipped_utc_entity)
    parsed = inputs_from_states(entities, cfg)
    assert not parsed.wake_known and not parsed.deadline_known and not parsed.prep_known
    assert parsed.wake_at is None and parsed.next_alarm is None
    cached, _ = step(DaemonState(), m(-38), win(), W)
    cached, _ = step(cached, m(-37), parsed, W)
    assert cached.active == WAKE and cached.deadline == D and cached.phase == "window"
    locked, _ = step(cached, m(-36), parsed, W)
    assert locked.active == WAKE and locked.deadline == D


def test_missing_snapshot_and_valid_none_are_distinct() -> None:
    cfg = AlarmConfig(schedule_contract="v1")
    entities = states(cfg)
    entities[cfg.enabled_entity] = {"state": "on"}
    entities.pop(cfg.skipped_utc_entity)
    entities.pop(cfg.schedule_entity)
    active, _ = step(DaemonState(), WAKE, win(), W)
    kept, _ = step(active, m(-36), inputs_from_states(entities, cfg), W)
    assert kept.active == WAKE
    entities[cfg.schedule_entity] = {"state": snapshot("none")}
    cleared, _ = step(kept, m(-35), inputs_from_states(entities, cfg), W)
    assert cleared.mode == "idle" and cleared.ended_wake is not None
    assert cleared.ended_wake.reason == "schedule_cleared"


def test_status_identity_rerings_and_continuous_final(tmp_path: Path) -> None:
    now = [WAKE]
    current = [win()]
    exports: list[dict[str, Any]] = []
    cache = AlarmCache(tmp_path / "cache")

    def export(payload: dict[str, Any]) -> bool:
        # The exact identity and revision must already be durable at publication.
        persisted = json.loads(cache.path.read_text())
        assert persisted["revision"] == payload["attributes"]["revision"]
        assert persisted["session"]["id"] == payload["attributes"]["session"]["id"]
        exports.append(payload)
        return True

    player = FakePlayer()
    daemon = AlarmDaemon(lambda: current[0], player, cache, W,
                         clock=lambda: now[0], publish_status=export)
    daemon.run_once()
    first = exports[-1]["attributes"]
    assert first["session"]["phase"] == "window"
    assert first["ring"]["playback"] == "running"
    assert status_observation_fresh(first["observed_at"], now[0], 30)
    now[0] = m(-36)
    current[0] = win(snooze_pressed_at=now[0])
    daemon.run_once()
    assert exports[-1]["state"] == "snoozed"
    assert exports[-1]["attributes"]["ring"]["playback"] == "stopped"
    now[0] = m(-31)
    current[0] = win()
    daemon.run_once()
    rering = exports[-1]["attributes"]
    assert rering["session"]["id"] == first["session"]["id"]
    assert rering["session"]["original_at"] == first["session"]["original_at"]
    assert rering["ring"]["id"] != first["ring"]["id"]
    # Advance through snoozes without timeout to get a continuous final transition.
    now[0] = m(-1)
    daemon.state = replace(daemon.state, ring_started=now[0])
    daemon.run_once()
    before = exports[-1]["attributes"]
    events = player.events.copy()
    now[0] = D
    daemon.run_once()
    final = exports[-1]["attributes"]
    assert final["session"]["phase"] == "final"
    assert final["ring"]["id"] == before["ring"]["id"]
    assert final["ring"]["started_at"] == before["ring"]["started_at"]
    assert player.events == events
    assert final["revision"] > before["revision"]


def test_slow_export_does_not_reset_monotonic_ramp_origin(tmp_path: Path) -> None:
    monotonic = [100.0]
    samples: list[tuple[float, float]] = []

    class ClockedPlayer(PipeWirePlayer):
        def start(self, profile: Any = None) -> None:
            # Fake startup only; retain the real player's monotonic curve evaluator.
            self._ramp_origin = self._monotonic()

        def tick(self) -> None:
            pass

    player = ClockedPlayer("unused sink", tmp_path / "unused.wav", initial_volume=0.2,
                           ramp_seconds=60, monotonic=lambda: monotonic[0])

    def export(_payload: dict[str, Any]) -> bool:
        elapsed = monotonic[0] - player._ramp_origin
        before = player._level(elapsed)
        monotonic[0] += 5  # elapsed playback time while the synchronous exporter waits
        after = player._level(monotonic[0] - player._ramp_origin)
        samples.append((before, after))
        return False

    daemon = AlarmDaemon(lambda: inp(), player, AlarmCache(tmp_path / "cache"), Settings(),
                         clock=lambda: T0, publish_status=export)
    daemon.run_once()
    daemon.run_once()
    assert player._ramp_origin == 100.0
    assert samples[0][0] == pytest.approx(0.2)
    assert samples[0][1] > samples[0][0]
    assert samples[1][0] == samples[0][1] and samples[1][1] > samples[1][0]


def test_failed_export_retries_current_snapshot_without_resetting_audio(tmp_path: Path) -> None:
    now = [T0]
    current = [inp()]
    exports: list[dict[str, Any]] = []

    def export(payload: dict[str, Any]) -> bool:
        exports.append(payload)
        time.sleep(0.01)  # fake bounded slow advisory HTTP
        raise httpx.ReadTimeout("fake status timeout")

    player = FakePlayer()
    daemon = AlarmDaemon(lambda: current[0], player, AlarmCache(tmp_path / "cache"), Settings(),
                         clock=lambda: now[0], publish_status=export)
    daemon.run_once()
    original = daemon.state.session
    ring = daemon.state.ring
    revision = daemon.state.revision
    now[0] = at(0.1)
    daemon.run_once()
    assert daemon.state.revision == revision  # failed delivery / successful checks are not changes
    assert daemon.state.session == original and daemon.state.ring is not None and ring is not None
    assert daemon.state.ring.id == ring.id and player.events.count("start") == 1
    current[0] = inp(enabled=False)
    daemon.run_once()
    assert daemon.state.revision > revision
    assert daemon.state.mode == "idle" and "stop" in player.events
    terminal = exports[-1]["attributes"]["ended"]["wake"]
    assert terminal["reason"] == "master_off"
    current[0] = inp(at(1), enabled=True)
    now[0] = at(1)
    daemon.run_once()
    latest = exports[-1]["attributes"]
    assert latest["session"]["id"] != terminal["session_id"]
    assert latest["ended"]["wake"] == terminal
    assert len(exports) == 4
    assert daemon.state.window_done_pending is None


@pytest.mark.parametrize("control,reason", [
    ("masteroff", "master_off"), ("skip", "skip"), ("activity", "desktop_activity"),
])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_status_never_gates_window_controls(
    tmp_path: Path, control: str, reason: str, raises: bool,
) -> None:
    now = [WAKE]
    current = [win()]
    player = FakePlayer()
    calls: list[str] = []

    def export(_payload: dict[str, Any]) -> bool:
        # Even the failed attempt must happen after current-poll controls and persistence.
        calls.append(daemon.state.mode)
        if len(calls) == 2:
            assert daemon.state.mode == "idle" and "stop" in player.events
            assert daemon.cache.load().ended_wake == daemon.state.ended_wake
        if raises:
            raise httpx.ReadTimeout("fake advisory export timeout")
        return False

    daemon = AlarmDaemon(lambda: current[0], player, AlarmCache(tmp_path / "cache"), W,
                         clock=lambda: now[0], publish_status=export)
    daemon.run_once()
    now[0] = m(-36)
    controls: dict[str, dict[str, Any]] = {"masteroff": {"enabled": False}, "skip": {"skipped_at": WAKE},
                "activity": {"activity_at": now[0]}}
    current[0] = win(**controls[control])
    daemon.run_once()
    assert calls == ["ringing", "idle"]
    assert daemon.state.mode == "idle" and "stop" in player.events
    assert daemon.state.ended_wake is not None and daemon.state.ended_wake.reason == reason


@pytest.mark.parametrize("failure", ["start", "tick"])
def test_audio_failure_is_not_terminal_or_running(tmp_path: Path, failure: str) -> None:
    class Broken(FakePlayer):
        def start(self, profile: Any = None) -> None:
            if failure == "start":
                raise RuntimeError("fake start failure")
            super().start(profile)

        def tick(self) -> None:
            if failure == "tick":
                raise RuntimeError("fake tick failure")

    exports: list[dict[str, Any]] = []
    cache = AlarmCache(tmp_path / "cache")
    daemon = AlarmDaemon(lambda: inp(), Broken(), cache, Settings(), clock=lambda: T0,
                         publish_status=recorder(exports))
    daemon.run_once()
    attrs = exports[-1]["attributes"]
    assert attrs["audio_failed"] and attrs["ring"]["playback"] == "failed"
    assert attrs["ended"] == {"wake": None, "nap": None}
    restored = cache.load()
    assert restored.session == daemon.state.session
    assert restored.ring is not None and restored.ring.playback == "starting"


def test_existing_audio_worker_error_becomes_failed_status_without_crashing(tmp_path: Path) -> None:
    # Exercise the real worker-error health probe, without starting a thread or audio process.
    probe = PipeWirePlayer("unused sink", tmp_path / "unused.wav")
    probe._error = RuntimeError("fake local audio worker failure")

    class WorkerFailure(FakePlayer):
        def tick(self) -> None:
            probe.tick()

    exports: list[dict[str, Any]] = []
    beats: list[Any] = []
    cache = AlarmCache(tmp_path / "cache")
    player = WorkerFailure()
    daemon = AlarmDaemon(lambda: inp(), player, cache, Settings(), clock=lambda: T0,
                         heartbeat=recorder(beats),
                         publish_status=recorder(exports))
    assert daemon.run_once() == 2.0
    assert player.events == ["start", "stop"] and beats == []
    assert exports[-1]["attributes"]["audio_failed"] is True
    assert exports[-1]["attributes"]["ring"]["playback"] == "failed"
    assert daemon.state.ended_wake is None
    restored = cache.load()
    assert restored.audio_failed and restored.ring is not None
    assert restored.ring.playback == "starting"


def test_unchanged_semantic_revision_refreshes_export_observation_and_health_check(tmp_path: Path) -> None:
    now = [T0]
    exports: list[dict[str, Any]] = []
    daemon = AlarmDaemon(lambda: inp(), FakePlayer(), AlarmCache(tmp_path / "cache"), Settings(),
                         clock=lambda: now[0],
                         publish_status=recorder(exports))
    daemon.run_once()
    now[0] = at(0.1)
    daemon.run_once()
    first, second = (payload["attributes"] for payload in exports)
    assert first["revision"] == second["revision"] == daemon.cache.load().revision
    assert first["observed_at"] != second["observed_at"]
    assert first["ring"]["checked_at"] != second["ring"]["checked_at"]
    assert first["ring"]["id"] == second["ring"]["id"]
    assert first["ring"]["started_at"] == second["ring"]["started_at"]
    assert first["session"] == second["session"]


def test_revision_tracks_health_changes_not_repeated_checks(tmp_path: Path) -> None:
    broken = [False]
    now = [T0]

    class Player(FakePlayer):
        def tick(self) -> None:
            if broken[0]:
                raise RuntimeError("fake tick failure")

    daemon = AlarmDaemon(lambda: inp(), Player(), AlarmCache(tmp_path / "cache"), Settings(),
                         clock=lambda: now[0])
    daemon.run_once()
    healthy_revision = daemon.state.revision
    now[0] = at(0.1)
    daemon.run_once()
    assert daemon.state.revision == healthy_revision
    assert daemon.state.ring is not None and daemon.state.ring.checked_at == now[0]
    broken[0] = True
    now[0] = at(0.2)
    daemon.run_once()
    failed_revision = daemon.state.revision
    assert failed_revision == healthy_revision + 1
    assert daemon.state.audio_failed
    now[0] = at(0.3)
    daemon.run_once()
    assert daemon.state.revision == failed_revision
    assert daemon.cache.load().revision == failed_revision


def test_restart_preserves_identity_but_not_running_health(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "cache")
    daemon = AlarmDaemon(lambda: inp(), FakePlayer(), cache, Settings(), clock=lambda: T0)
    daemon.run_once()
    old = daemon.state
    resumed = AlarmDaemon(lambda: Inputs(ha_ok=False), FakePlayer(), cache, Settings(), clock=lambda: at(1))
    assert resumed.state.ring is not None and resumed.state.ring.playback == "starting"
    resumed.run_once()
    assert resumed.state.session == old.session
    assert resumed.state.ring is not None and old.ring is not None
    assert resumed.state.ring.id == old.ring.id and resumed.state.ring.started_at == old.ring.started_at
    ring = resumed.state.ring  # re-read: run_once replaces the ring snapshot
    assert ring is not None and ring.playback == "running"


@pytest.mark.parametrize("control,reason", [("skip", "skip"), ("clear", "schedule_cleared"),
                                             ("timeout", "legacy_timeout"), ("stop", "legacy_stop"),
                                             ("activity", "desktop_activity"), ("replace", "schedule_replaced")])
def test_legacy_terminal_records(control: str, reason: str) -> None:
    active, _ = step(DaemonState(), T0, inp(), Settings())
    controls = {"skip": inp(skipped_at=T0), "clear": inp(None),
                "timeout": inp(), "stop": inp(stop_pressed_at=at(1)),
                "activity": inp(activity_at=at(1)), "replace": inp(at(2))}
    state, _ = step(active, at(16) if control == "timeout" else at(1), controls[control], Settings())
    assert state.ended_wake is not None and active.session is not None
    assert state.ended_wake.reason == reason and state.ended_wake.session_id == active.session.id
    assert state.ended_wake.phase == "legacy"


def test_tomorrow_rollover_does_not_mask_desktop_activity_terminal() -> None:
    active, _ = step(DaemonState(), WAKE, win(), W)
    ended, _ = step(active, D + timedelta(hours=1), win(
        deadline=D + timedelta(days=1), wake=WAKE + timedelta(days=1),
        activity_at=D + timedelta(hours=1),
    ), W)
    assert ended.ended_wake is not None and ended.ended_wake.reason == "desktop_activity"
    legacy, _ = step(DaemonState(), T0, inp(), Settings())
    stopped, _ = step(legacy, at(1), inp(at(1440), activity_at=at(1)), Settings())
    assert stopped.ended_wake is not None and stopped.ended_wake.reason == "desktop_activity"


def test_master_off_records_pending_session_terminal() -> None:
    pending, _ = step(DaemonState(), at(-1), inp(), Settings())
    ended, _ = step(pending, at(-0.5), inp(enabled=False), Settings())
    assert ended.mode == "idle" and ended.ended_wake is not None
    assert ended.ended_wake.original_at == T0 and ended.ended_wake.reason == "master_off"
    # Advisory terminal metadata does not change legacy re-enable scheduling.
    enabled, _ = step(ended, T0, inp(enabled=True), Settings())
    assert enabled.mode == "ringing" and enabled.ended_wake == ended.ended_wake


def test_expired_recovery_records_terminal(tmp_path: Path) -> None:
    active, _ = step(DaemonState(), WAKE, win(), W)
    cache = AlarmCache(tmp_path / "cache")
    cache.save(active)
    daemon = AlarmDaemon(lambda: Inputs(ha_ok=False), FakePlayer(), cache, W,
                         clock=lambda: D + timedelta(minutes=11))
    daemon.run_once()
    assert daemon.state.mode == "idle" and daemon.state.ended_wake is not None
    assert daemon.state.ended_wake.reason == "recovery_expired"
    assert active.session is not None and daemon.state.ended_wake.session_id == active.session.id


@pytest.mark.parametrize("payload", [
    {"session": [], "ring": [], "ended_wake": [], "revision": []},
    {"session": {"id": "x", "original_at": "bad", "kind": []}, "phase": []},
    {"ring": {"id": [], "started_at": T0.isoformat()}, "ended_nap": {"reason": []}},
])
def test_malformed_status_cache_does_not_poison_legacy_cache(tmp_path: Path, payload: dict[str, Any]) -> None:
    cache = AlarmCache(tmp_path / "cache")
    cache.path.write_text(json.dumps({
        "mode": "ringing", "active": T0.isoformat(), "active_kind": "wake",
        "ring_started": T0.isoformat(), "pending": at(100).isoformat(), **payload,
    }))
    loaded = cache.load()
    assert loaded.active == T0 and loaded.pending == at(100)
    assert loaded.session is None and loaded.ring is None
    assert loaded.revision == 0 and loaded.ended_wake is None and loaded.ended_nap is None


@pytest.mark.parametrize("field", ["original_at", "kind", "phase", "locked_deadline_at", "ring_started_at"])
def test_valid_shaped_incoherent_cache_metadata_is_regenerated(tmp_path: Path, field: str) -> None:
    active, _ = step(DaemonState(), WAKE, win(), W)
    assert active.session is not None and active.ring is not None
    # Retained terminal metadata must never become the source of a new active identity.
    terminal, _ = step(active, m(-36), win(activity_at=m(-36)), W)
    cache = AlarmCache(tmp_path / "cache")
    cache.save(replace(active, ended_wake=terminal.ended_wake))
    raw = json.loads(cache.path.read_text())
    replacements = {"original_at": m(-38).isoformat(), "kind": "nap", "phase": "final",
                    "locked_deadline_at": m(1).isoformat()}
    if field == "ring_started_at":
        raw["ring"]["started_at"] = m(-38).isoformat()
    else:
        raw["session"][field] = replacements[field]
    cache.path.write_text(json.dumps(raw))
    loaded = cache.load()
    assert loaded.active == WAKE and loaded.phase == "window" and loaded.deadline == D
    assert loaded.mode == "ringing" and loaded.ring_started == WAKE
    assert loaded.ring is None
    assert (loaded.session is not None) is (field == "ring_started_at")
    player = FakePlayer()
    daemon = AlarmDaemon(lambda: Inputs(ha_ok=False), player, cache, W, clock=lambda: m(-36))
    daemon.run_once()
    assert daemon.state.active == WAKE and daemon.state.deadline == D
    assert daemon.state.session is not None and daemon.state.ring is not None
    assert daemon.state.session.original_at == WAKE and daemon.state.session.kind == "wake"
    assert daemon.state.session.phase == "window" and daemon.state.session.locked_deadline_at == D
    if field != "ring_started_at":
        assert daemon.state.session.id != active.session.id
    else:
        assert daemon.state.session.id == active.session.id
    assert daemon.state.ring.id != active.ring.id and daemon.state.ring.started_at == WAKE
    assert daemon.state.ended_wake == terminal.ended_wake
    assert player.events == ["start"]


@pytest.mark.parametrize("field", ["original_at", "locked_deadline_at", "ring_started_at"])
def test_cache_identity_coherence_distinguishes_dst_folds(tmp_path: Path, field: str) -> None:
    from datetime import UTC, datetime

    from commutecompass.timeutil import NYC_TZ

    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).astimezone(NYC_TZ)
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC).astimezone(NYC_TZ)
    # Python's same-zone equality cannot distinguish these different instants.
    assert first == second
    session = {"id": "a" * 32, "original_at": first.isoformat(), "kind": "wake",
               "phase": "window", "locked_deadline_at": first.isoformat()}
    raw: dict[str, Any] = {
        "mode": "ringing", "active": first.isoformat(), "active_kind": "wake",
        "ring_started": first.isoformat(), "deadline": first.isoformat(), "phase": "window",
        "session": session, "ring": {"id": "b" * 32, "started_at": first.isoformat()},
    }
    if field == "ring_started_at":
        raw["ring"]["started_at"] = second.isoformat()
    else:
        session[field] = second.isoformat()
    cache = AlarmCache(tmp_path / "cache")
    cache.path.write_text(json.dumps(raw))
    loaded = cache.load()
    assert loaded.ring is None
    assert (loaded.session is not None) is (field == "ring_started_at")
    assert loaded.active is not None and loaded.active.astimezone(UTC) == first.astimezone(UTC)


@pytest.mark.parametrize("metadata", ["old_cache", "rejected_session"])
def test_first_poll_master_off_records_terminal_for_cache_without_coherent_identity(
    tmp_path: Path, metadata: str,
) -> None:
    cache = AlarmCache(tmp_path / "cache")
    raw: dict[str, Any] = {
        "mode": "ringing", "active": T0.isoformat(), "active_kind": "wake",
        "ring_started": T0.isoformat(),
    }
    if metadata == "rejected_session":
        raw["session"] = {"id": "a" * 32, "original_at": at(1).isoformat(),
                          "kind": "nap", "phase": "legacy", "locked_deadline_at": None}
        raw["ring"] = {"id": "b" * 32, "started_at": T0.isoformat()}
    cache.path.write_text(json.dumps(raw))
    assert cache.load().session is None
    exports: list[dict[str, Any]] = []
    player = FakePlayer()
    daemon = AlarmDaemon(lambda: inp(enabled=False), player, cache, Settings(), clock=lambda: at(1),
                         publish_status=recorder(exports))
    daemon.run_once()
    terminal = daemon.state.ended_wake
    assert daemon.state.mode == "idle" and terminal is not None
    assert terminal.original_at == T0 and terminal.kind == "wake" and terminal.phase == "legacy"
    assert terminal.reason == "master_off" and terminal.locked_deadline_at is None
    assert len(terminal.session_id) == 32 and terminal.session_id != "a" * 32
    assert daemon.cache.load().ended_wake == terminal
    assert exports[-1]["attributes"]["ended"]["wake"]["session_id"] == terminal.session_id
    assert "start" not in player.events


def test_status_periodic_recreation_and_outage_are_read_gated(tmp_path: Path) -> None:
    current = [Inputs(ha_ok=True)]
    exports: list[dict[str, Any]] = []
    daemon = AlarmDaemon(lambda: current[0], FakePlayer(), AlarmCache(tmp_path / "cache"), Settings(),
                         clock=lambda: T0, publish_status=recorder(exports))
    daemon.run_once()
    daemon.run_once()
    assert len(exports) == 2 and exports[-1]["state"] == "idle"
    assert exports[0]["attributes"]["revision"] == exports[1]["attributes"]["revision"] == 0
    current[0] = Inputs(ha_ok=False)
    daemon.run_once()
    assert len(exports) == 2
    current[0] = Inputs(ha_ok=True)
    daemon.run_once()
    assert len(exports) == 3 and exports[-1]["attributes"]["revision"] == daemon.cache.load().revision


def test_nap_termination_retains_latest_wake_terminal() -> None:
    wake, _ = step(DaemonState(), T0, inp(), Settings())
    ended, _ = step(wake, at(1), inp(stop_pressed_at=at(1)), Settings())
    nap_inputs = Inputs(ha_ok=True, nap_at=at(2), nap_known=True)
    nap, _ = step(ended, at(2), nap_inputs, Settings())
    stopped, _ = step(nap, at(3), replace(nap_inputs, skipped_at=at(2)), Settings())
    assert stopped.ended_wake == ended.ended_wake
    assert stopped.ended_nap is not None and stopped.ended_nap.reason == "skip"
    assert stopped.ended_nap.kind == "nap" and stopped.ended_nap.original_at == at(2)


def test_v1_effective_next_prep_cap_matches_legacy_without_subtracting_lead_twice() -> None:
    cfg = AlarmConfig(schedule_contract="v1", wake_lead_minutes=15)
    raw = serialize_schedule(new_schedule(
        kind="legacy", issued_at=at(-60), wake_at=at(100), deadline_at=None,
        next_prep_at=at(10),  # publisher's effective first-alarm cap, not raw prep
    ))
    entities = states(cfg, raw)
    entities[cfg.enabled_entity] = {"state": "on"}
    entities[cfg.nap_utc_entity] = {"state": T0.isoformat()}
    entities.pop(cfg.skipped_utc_entity)
    entities.pop(cfg.stop_button)
    entities.pop(cfg.snooze_button)
    v1 = inputs_from_states(entities, cfg)
    assert v1.prep_threshold == at(10)  # nonzero wake lead must not shift the published cap
    legacy_cfg = cfg.model_copy(update={"schedule_contract": "legacy"})
    entities[cfg.wake_utc_entity] = {"state": at(100).isoformat()}
    entities[cfg.next_prep_utc_entity] = {"state": at(10).isoformat()}
    legacy = inputs_from_states(entities, legacy_cfg)
    for inputs in (v1, legacy):
        active, _ = step(DaemonState(), T0, inputs, Settings())
        snoozed, _ = step(active, at(2), replace(inputs, snooze_pressed_at=at(2)), Settings())
        assert snoozed.active_kind == "nap" and snoozed.snooze_until == at(10)


def test_v1_effective_prep_cap_preserves_instant_across_dst() -> None:
    from datetime import UTC, datetime

    from commutecompass.timeutil import NYC_TZ

    # Effective cap in the second 01:00 hour must not shift to the first occurrence.
    prep = datetime(2026, 11, 1, 6, 10, tzinfo=UTC).astimezone(NYC_TZ)
    raw = serialize_schedule(new_schedule(
        kind="legacy", issued_at=at(-60), wake_at=at(100), deadline_at=None,
        next_prep_at=prep,
    ))
    cfg = AlarmConfig(schedule_contract="v1", wake_lead_minutes=15)
    parsed = inputs_from_states(states(cfg, raw), cfg)
    assert parsed.prep_threshold is not None
    assert parsed.prep_threshold.astimezone(UTC) == datetime(2026, 11, 1, 6, 10, tzinfo=UTC)


def test_v1_partial_legacy_writes_do_not_change_snapshot() -> None:
    cfg = AlarmConfig(schedule_contract="v1")
    entities = states(cfg)
    expected = inputs_from_states(entities, cfg)
    for entity in (cfg.wake_utc_entity, cfg.window.deadline_utc_entity, cfg.next_prep_utc_entity,
                   cfg.next_alarm_entity):
        entities[entity] = {"state": "unavailable"}
        actual = inputs_from_states(entities, cfg)
        assert actual == expected


@pytest.mark.parametrize("outcome", ["timeout", "ok", "error"])
def test_status_http_is_optional_bounded_and_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    calls: list[httpx.Request] = []
    timeouts: list[httpx.Timeout] = []
    closed: list[bool] = []
    real_client = httpx.Client

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            raise AssertionError("status response body must never be consumed")

        def close(self) -> None:
            closed.append(True)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("fake timeout")
        return httpx.Response(201 if outcome == "ok" else 503, stream=UnreadBody())

    def client(*, timeout: httpx.Timeout) -> httpx.Client:
        timeouts.append(timeout)
        return real_client(timeout=timeout, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("commutecompass.alarm_daemon.synthesize_tone", lambda _path: tmp_path / "fake.wav")
    monkeypatch.setattr(httpx, "Client", client)
    cfg = Config.model_construct(
        home_assistant=HomeAssistantConfig(enabled=True, base_url="http://ha.test"),
        home_assistant_token="fake", alarm=AlarmConfig(
            status_entity="sensor.commute_alarm_status", cache_path=str(tmp_path / "cache"),
        ),
    )
    daemon = build_daemon(cfg)
    assert daemon.publish_status is not None
    assert daemon.publish_status(status_snapshot(DaemonState(), T0)) is (outcome == "ok")
    assert len(calls) == 1
    assert str(calls[0].url) == "http://ha.test/api/states/sensor.commute_alarm_status"
    assert calls[0].method == "POST"
    assert len(timeouts) == 1
    assert timeouts[0].as_dict() == {"connect": 0.5, "read": 0.5, "write": 0.5, "pool": 0.5}
    if outcome != "timeout":
        assert closed == [True]
    cfg.alarm.status_entity = ""
    assert build_daemon(cfg).publish_status is None
