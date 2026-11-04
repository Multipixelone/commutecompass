"""Tests for alarm_daemon.py — state machine, cache, HA parsing, fake audio."""

from __future__ import annotations

import json
import wave
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    DaemonState,
    HAReader,
    Inputs,
    PipeWirePlayer,
    PublishState,
    RingProfile,
    Settings,
    StartRing,
    StopRing,
    build_daemon,
    find_sink,
    inputs_from_states,
    parse_wpctl_volume,
    ramp_fraction,
    step,
    synthesize_tone,
)
from commutecompass.config import AlarmConfig, Config, HomeAssistantConfig
from commutecompass.timeutil import NYC_TZ

T0 = datetime(2026, 10, 15, 7, 0, tzinfo=NYC_TZ)
S = Settings()


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def inp(alarm: datetime | None = T0, **kw: Any) -> Inputs:
    return Inputs(ha_ok=True, next_alarm=alarm, kind="wake",
                  wake_at=alarm, wake_known=True, **kw)


def kinds(actions: list[Any]) -> list[str]:
    out = []
    for a in actions:
        if isinstance(a, StartRing):
            out.append("start")
        elif isinstance(a, StopRing):
            out.append(f"stop:{a.reason}")
        elif isinstance(a, PublishState):
            out.append(f"state:{a.mode}")
    return out


# ─────────── State machine ────────────────────────────────────────────────────


def test_idle_until_alarm_time() -> None:
    st, acts = step(DaemonState(), at(-1), inp(), S)
    assert st.mode == "idle" and acts == []
    assert st.pending == T0


def test_rings_at_alarm_time() -> None:
    st, _ = step(DaemonState(), at(-1), inp(), S)
    st, acts = step(st, at(0), inp(), S)
    assert st.mode == "ringing"
    assert kinds(acts) == ["start", "state:ringing"]
    assert st.ring_started == at(0)


def test_rings_from_pending_even_when_ha_moved_on() -> None:
    """HA's sensor only lists future alarms; a remembered instant still rings."""
    st, _ = step(DaemonState(), at(-1), inp(), S)
    st, _acts = step(st, at(0.5), inp(at(1440)), S)
    assert st.mode == "ringing"
    assert st.active == T0
    assert st.pending == at(1440)


def test_stop_returns_to_idle_and_never_rerings() -> None:
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, acts = step(st, at(1), inp(stop_pressed_at=at(0.5)), S)
    assert st.mode == "idle"
    assert kinds(acts) == ["stop:stop", "state:idle"]
    # HA still reports the same instant (lagging sensor) → no re-ring.
    st, acts = step(st, at(2), inp(stop_pressed_at=at(0.5)), S)
    assert acts == [] and st.mode == "idle"


def test_stop_pressed_before_ring_is_ignored() -> None:
    """Button ``state`` is last-pressed time; a press from yesterday doesn't stop today."""
    st, _ = step(DaemonState(), at(0), inp(stop_pressed_at=at(-600)), S)
    st, acts = step(st, at(1), inp(stop_pressed_at=at(-600)), S)
    assert st.mode == "ringing" and acts == []


def test_snooze_up_to_max_then_ignored() -> None:
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, acts = step(st, at(1), inp(snooze_pressed_at=at(0.9)), S)
    assert st.mode == "snoozed" and st.snoozes == 1
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.snooze_until == at(10)

    st, acts = step(st, at(5), inp(snooze_pressed_at=at(0.9)), S)
    assert st.mode == "snoozed" and acts == []
    st, acts = step(st, at(10), inp(snooze_pressed_at=at(0.9)), S)
    assert st.mode == "ringing" and kinds(acts) == ["start", "state:ringing"]

    st, _ = step(st, at(11), inp(snooze_pressed_at=at(10.5)), S)
    assert st.mode == "snoozed" and st.snoozes == 2
    st, _ = step(st, at(20), inp(snooze_pressed_at=at(10.5)), S)
    assert st.mode == "ringing"

    # Third snooze exceeds max_snoozes=2 → ignored, keeps ringing.
    st, acts = step(st, at(21), inp(snooze_pressed_at=at(20.5)), S)
    assert st.mode == "ringing" and acts == []
    # Stop still works.
    st, acts = step(st, at(22), inp(stop_pressed_at=at(21.5)), S)
    assert st.mode == "idle"


def test_stop_while_snoozed_cancels() -> None:
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, _ = step(st, at(1), inp(snooze_pressed_at=at(0.5)), S)
    st, acts = step(st, at(3), inp(stop_pressed_at=at(2)), S)
    assert st.mode == "idle" and kinds(acts) == ["state:idle"]
    st, acts = step(st, at(10), inp(stop_pressed_at=at(2)), S)
    assert acts == []


def test_max_ring_timeout() -> None:
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, acts = step(st, at(14.9), inp(), S)
    assert st.mode == "ringing" and acts == []
    st, acts = step(st, at(15), inp(), S)
    assert st.mode == "idle"
    assert kinds(acts) == ["stop:timeout", "state:idle"]
    st, acts = step(st, at(16), inp(), S)
    assert acts == []


def test_stale_on_startup_skipped() -> None:
    cached = DaemonState(pending=T0, pending_kind="wake")
    st, acts = step(cached, at(11), Inputs(ha_ok=False), S)
    assert st.mode == "idle" and acts == []
    assert st.is_done(T0)


def test_slightly_late_on_startup_still_rings() -> None:
    cached = DaemonState(pending=T0, pending_kind="wake")
    st, _acts = step(cached, at(9), Inputs(ha_ok=False), S)
    assert st.mode == "ringing"


def test_cache_fallback_when_ha_down(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(-60), inp(), S)
    cache.save(st)
    # Restart with HA unreachable.
    st2 = cache.load()
    assert st2.pending == T0
    st2, _acts = step(st2, at(-1), Inputs(ha_ok=False), S)
    assert st2.mode == "idle" and st2.pending == T0
    st2, _acts = step(st2, at(0), Inputs(ha_ok=False), S)
    assert st2.mode == "ringing"


def test_never_reruns_same_instant_after_restart(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, _ = step(st, at(1), inp(stop_pressed_at=at(0.5)), S)
    cache.save(st)
    st2 = cache.load()
    st2, acts = step(st2, at(2), inp(), S)
    assert acts == [] and st2.mode == "idle"


def test_disabled_stops_ringing_and_clears_pending() -> None:
    st, _ = step(DaemonState(), at(0), inp(), S)
    st, acts = step(st, at(1), inp(enabled=False), S)
    assert st.mode == "idle" and st.pending is None
    assert kinds(acts) == ["stop:disabled", "state:idle"]


def test_withdrawn_alarm_clears_pending() -> None:
    st, _ = step(DaemonState(), at(-5), inp(), S)
    st, acts = step(st, at(-4), inp(None), S)
    assert st.pending is None
    st, acts = step(st, at(0), inp(None), S)
    assert acts == []


def test_ha_down_keeps_pending() -> None:
    st, _ = step(DaemonState(), at(-5), inp(), S)
    st, _ = step(st, at(-4), Inputs(ha_ok=False), S)
    assert st.pending == T0


def test_ramp_fraction() -> None:
    assert ramp_fraction(timedelta(0), 30, 120) == pytest.approx(0.3)
    assert ramp_fraction(timedelta(seconds=60), 30, 120) == pytest.approx(0.65)
    assert ramp_fraction(timedelta(seconds=500), 30, 120) == 1.0
    assert ramp_fraction(timedelta(0), 30, 0) == 1.0


# ─────────── HA parsing ───────────────────────────────────────────────────────


def _states(**overrides: Any) -> dict[str, dict[str, Any]]:
    cfg = AlarmConfig()
    base: dict[str, dict[str, Any]] = {
        cfg.next_alarm_entity: {"state": "2026-10-15T07:00:00-04:00", "attributes": {"kind": "nap"}},
        cfg.wake_utc_entity: {"state": T0.isoformat()},
        cfg.enabled_entity: {"state": "on"},
        cfg.stop_button: {"state": "2026-10-14T07:03:00+00:00"},
        cfg.snooze_button: {"state": "unknown"},
    }
    base.update(overrides)
    return base


def test_inputs_from_states() -> None:
    i = inputs_from_states(_states(), AlarmConfig())
    assert i.ha_ok and i.enabled
    assert i.next_alarm == T0
    assert i.kind == "nap"
    assert i.stop_pressed_at is not None and i.stop_pressed_at.tzinfo is not None
    assert i.snooze_pressed_at is None


def test_inputs_unknown_and_disabled() -> None:
    cfg = AlarmConfig()
    i = inputs_from_states(
        _states(**{cfg.next_alarm_entity: {"state": "unknown"}, cfg.enabled_entity: {"state": "off"}}),
        cfg,
    )
    assert i.next_alarm is None and not i.enabled


def test_ha_reader_down_returns_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __init__(self, **_: Any) -> None: ...
        def __enter__(self) -> Self:
            return self
        def __exit__(self, *_: object) -> None: ...
        def get(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("down")

    monkeypatch.setattr("commutecompass.alarm_daemon.httpx.Client", Boom)
    assert HAReader("http://ha", "tok", AlarmConfig()).read() == Inputs(ha_ok=False)


def test_ha_reader_parses_list(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"entity_id": k, **v} for k, v in _states().items()]

    class Ok:
        def __init__(self, **_: Any) -> None: ...
        def __enter__(self) -> Self:
            return self
        def __exit__(self, *_: object) -> None: ...
        def get(self, url: str, **__: Any) -> httpx.Response:
            assert url == "http://ha/api/states"
            return httpx.Response(200, json=payload)

    monkeypatch.setattr("commutecompass.alarm_daemon.httpx.Client", Ok)
    assert HAReader("http://ha/", "tok", AlarmConfig()).read().next_alarm == T0


def test_cache_tolerates_garbage(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert AlarmCache(p).load() == DaemonState()


# ─────────── Audio helpers (no real subprocess) ──────────────────────────────


def test_find_sink_substring() -> None:
    dump = [
        {"id": 40, "info": {"props": {"media.class": "Audio/Sink", "node.name": "snapcast",
                                       "node.description": "Living Room"}}},
        {"id": 52, "info": {"props": {"media.class": "Audio/Sink",
                                       "node.name": "alsa_output.pci.analog-stereo",
                                       "node.description": "Built-in Audio Analog Stereo"}}},
    ]
    assert find_sink(dump, "ANALOG") == (52, "alsa_output.pci.analog-stereo")
    assert find_sink(dump, "living") == (40, "snapcast")
    assert find_sink(dump, "hdmi") is None


def test_parse_wpctl_volume() -> None:
    assert parse_wpctl_volume("Volume: 0.40\n") == (0.4, False)
    assert parse_wpctl_volume("Volume: 0.25 [MUTED]\n") == (0.25, True)


class _Proc:
    def __init__(self) -> None:
        self.terminated = False
        self.done = False

    def poll(self) -> int | None:
        return 0 if self.done else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        import subprocess
        if not self.done and not self.terminated:
            raise subprocess.TimeoutExpired("pw-play", timeout)
        return 0

    def kill(self) -> None: ...


def test_pipewire_player_saves_ramps_restores(tmp_path: Path) -> None:
    import subprocess

    calls: list[list[str]] = []
    procs: list[_Proc] = []
    dump = json.dumps([{"id": 52, "info": {"props": {"media.class": "Audio/Sink",
                                                      "node.name": "alsa.analog"}}}])

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = dump if args[0] == "pw-dump" else ("Volume: 0.20 [MUTED]" if args[1] == "get-volume" else "")
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    def popen(args: list[str], **_: Any) -> _Proc:
        calls.append(args)
        procs.append(_Proc())
        return procs[-1]

    p = PipeWirePlayer("analog", tmp_path / "t.wav", run=run, popen=popen)  # type: ignore[arg-type]
    p.start()
    assert ["wpctl", "set-mute", "52", "0"] in calls
    assert ["pw-play", "--target", "alsa.analog", str(tmp_path / "t.wav")] in calls
    p.tick()
    assert ["wpctl", "set-volume", "52", "0.3000"] in calls
    p.stop()
    assert procs[0].terminated
    assert calls[-2:] == [["wpctl", "set-volume", "52", "0.20"], ["wpctl", "set-mute", "52", "1"]]
    n = len(calls)
    p.stop()  # idempotent
    assert len(calls) == n


def test_synthesize_tone(tmp_path: Path) -> None:
    path = synthesize_tone(tmp_path / "x" / "tone.wav")
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getnframes() > 0
    mtime = path.stat().st_mtime_ns
    synthesize_tone(path)
    assert path.stat().st_mtime_ns == mtime


# ─────────── Daemon loop with fakes ───────────────────────────────────────────


def _recorder(sink: list[Any]) -> Any:
    def record(value: Any) -> bool:
        sink.append(value)
        return True

    return record


class FakePlayer:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def start(self, profile: RingProfile | None = None) -> None:
        self.events.append("start")

    def tick(self) -> None:
        pass

    def stop(self) -> None:
        self.events.append("stop")


def test_daemon_loop_end_to_end(tmp_path: Path) -> None:
    clock = [at(-1)]
    feed = [inp()]
    published: list[str] = []
    beats: list[datetime] = []
    player = FakePlayer()
    d = AlarmDaemon(
        reader=lambda: feed[0],
        player=player,
        cache=AlarmCache(tmp_path / "c.json"),
        settings=S,
        publish_state=_recorder(published),
        heartbeat=_recorder(beats),
        clock=lambda: clock[0],
    )
    assert d.run_once() == 30.0
    assert json.loads((tmp_path / "c.json").read_text())["pending"] == T0.isoformat()
    clock[0] = at(0)
    assert d.run_once() == 2.0
    assert player.events == ["start"]
    clock[0] = at(1)
    d.run_once()
    assert player.events == ["start"]  # HA polls no longer drive volume
    feed[0] = inp(stop_pressed_at=at(0.9))
    clock[0] = at(1.1)
    assert d.run_once() == 30.0
    assert player.events[-1] == "stop"
    assert published == ["idle", "ringing", "idle"]
    # Heartbeat throttled to once per poll_seconds.
    assert beats == [at(-1), at(0), at(1)]  # at(1.1) is 6 s later → skipped


def test_daemon_restores_audio_on_exit(tmp_path: Path) -> None:
    clock = [at(0)]
    player = FakePlayer()
    published: list[str] = []
    d: AlarmDaemon

    def sleep(_s: float) -> None:
        d.request_stop()

    d = AlarmDaemon(
        reader=lambda: inp(),
        player=player,
        cache=AlarmCache(tmp_path / "c.json"),
        settings=S,
        publish_state=_recorder(published),
        clock=lambda: clock[0],
        sleep=sleep,
    )
    d.run_forever()
    assert player.events[0] == "start"
    assert player.events[-1] == "stop"
    assert published[-1] == "ringing"


def test_audio_failure_suppresses_healthy_heartbeat(tmp_path: Path) -> None:
    class BrokenPlayer(FakePlayer):
        def start(self, profile: RingProfile | None = None) -> None:
            raise RuntimeError("sink missing")

    beats: list[Any] = []
    player = BrokenPlayer()
    daemon = AlarmDaemon(
        reader=lambda: inp(), player=player,
        cache=AlarmCache(tmp_path / "c.json"), settings=S,
        heartbeat=_recorder(beats), clock=lambda: at(0),
    )
    daemon.run_once()
    assert beats == []
    assert "stop" in player.events


def test_snooze_across_dst_uses_elapsed_minutes() -> None:
    from datetime import UTC

    start = datetime(2026, 11, 1, 1, 55, tzinfo=NYC_TZ)
    pressed = datetime(2026, 11, 1, 1, 56, tzinfo=NYC_TZ)
    st, _ = step(DaemonState(), start, inp(start), S)
    st, _ = step(st, pressed, inp(start, snooze_pressed_at=pressed), S)
    assert st.snooze_until is not None
    assert st.snooze_until.astimezone(UTC) - pressed.astimezone(UTC) == timedelta(minutes=9)
    assert st.snooze_until.fold == 1


def helper_inputs(
    *, wake: datetime | None = T0, nap: datetime | None = None,
    sensor: datetime | None = None, skip: datetime | None = None,
    enabled: bool = True, **kw: Any,
) -> Inputs:
    kw.setdefault("prep_known", "prep_threshold" in kw)
    return Inputs(ha_ok=True, next_alarm=sensor, kind="wake", enabled=enabled,
                  wake_at=wake, nap_at=nap, skipped_at=skip,
                  wake_known=True, nap_known=True, **kw)


@pytest.mark.parametrize("sensor", [None, at(1440)])
def test_future_only_sensor_startup_at_plus_30s(sensor: datetime | None) -> None:
    st, acts = step(DaemonState(), at(0.5), helper_inputs(sensor=sensor), S)
    assert st.mode == "ringing" and "start" in kinds(acts)
    assert st.active == T0


def test_pending_elapsed_survives_sensor_unavailable_without_helpers() -> None:
    st, _ = step(DaemonState(), at(-1), inp(), S)
    st, acts = step(st, at(0.5), Inputs(ha_ok=True), S)
    assert st.mode == "ringing" and "start" in kinds(acts)


def test_backing_reschedule_replaces_pending() -> None:
    st, _ = step(DaemonState(), at(-1), helper_inputs(sensor=T0), S)
    st, acts = step(st, at(0.5), helper_inputs(wake=at(60), sensor=at(60)), S)
    assert st.mode == "idle" and st.pending == at(60) and not acts


def test_cleared_schedule_persisted_then_outage(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(-1), helper_inputs(sensor=T0), S)
    st, _ = step(st, at(-0.5), helper_inputs(wake=None), S)
    cache.save(st)
    assert json.loads(cache.path.read_text())["pending"] is None
    st, acts = step(cache.load(), at(0.5), Inputs(ha_ok=False), S)
    assert st.pending is None and st.mode == "idle" and not acts


@pytest.mark.parametrize("disabled", [True, False])
def test_skip_or_disable_then_outage_never_rings(tmp_path: Path, disabled: bool) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(-1), helper_inputs(sensor=T0), S)
    st, _ = step(st, at(-0.5), helper_inputs(enabled=not disabled, skip=None if disabled else T0), S)
    cache.save(st)
    st, acts = step(cache.load(), at(0.5), Inputs(ha_ok=False), S)
    assert st.mode == "idle" and not acts
    if disabled:
        assert not st.enabled


@pytest.mark.parametrize("snoozed", [False, True])
@pytest.mark.parametrize("disabled", [True, False])
def test_skip_disable_stops_active_ring_or_snooze(snoozed: bool, disabled: bool) -> None:
    st, _ = step(DaemonState(), at(0), helper_inputs(), S)
    if snoozed:
        st, _ = step(st, at(1), helper_inputs(snooze_pressed_at=at(0.5)), S)
    st, acts = step(st, at(2), helper_inputs(enabled=not disabled, skip=None if disabled else T0), S)
    assert st.mode == "idle" and st.snooze_until is None
    assert "state:idle" in kinds(acts)
    if not snoozed:
        assert any(isinstance(a, StopRing) for a in acts)


def test_snooze_persists_deadline_count_restart(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(0), helper_inputs(), S)
    st, _ = step(st, at(1), helper_inputs(snooze_pressed_at=at(0.5)), S)
    cache.save(st)
    st = cache.load()
    assert st.mode == "snoozed" and st.snoozes == 1 and st.snooze_until == at(10)
    st, acts = step(st, at(9), Inputs(ha_ok=False), S)
    assert st.mode == "snoozed" and not acts
    st, acts = step(st, at(10), Inputs(ha_ok=False), S)
    assert st.mode == "ringing" and st.snoozes == 1 and kinds(acts) == ["start", "state:ringing"]


def test_snooze_capped_at_prep_threshold() -> None:
    st, _ = step(DaemonState(), at(0), helper_inputs(prep_threshold=at(4)), S)
    st, _ = step(st, at(1), helper_inputs(prep_threshold=at(4), snooze_pressed_at=at(0.5)), S)
    assert st.snooze_until == at(4)
    st, acts = step(st, at(4), helper_inputs(prep_threshold=at(4)), S)
    assert st.mode == "ringing" and "start" in kinds(acts)
    st, acts = step(st, at(5), helper_inputs(prep_threshold=at(4), snooze_pressed_at=at(4.5)), S)
    assert st.mode == "ringing" and not acts


def test_reader_effective_prep_cap_does_not_subtract_lead_twice() -> None:
    cfg = AlarmConfig(wake_lead_minutes=15)
    # Raw prep is +19 min; publisher has already applied the 15-minute lead.
    states = _states(**{cfg.next_prep_utc_entity: {"state": at(4).isoformat()},
                       cfg.wake_utc_entity: {"state": T0.isoformat()},
                       cfg.nap_utc_entity: {"state": "1970-01-01T00:00:00+00:00"}})
    i = inputs_from_states(states, cfg)
    assert i.prep_threshold == at(4) and i.wake_known and i.nap_known
    st, _ = step(DaemonState(), at(0), i, S)
    states[cfg.snooze_button] = {"state": at(0.5).isoformat()}
    st, _ = step(st, at(1), inputs_from_states(states, cfg), S)
    assert st.mode == "snoozed" and st.snooze_until == at(4)


def test_failure_latch_published_once_success_clears(tmp_path: Path) -> None:
    class Broken(FakePlayer):
        def start(self, profile: RingProfile | None = None) -> None:
            raise RuntimeError("missing sink")
    clock = [at(0)]
    feed = [inp()]
    published: list[Any] = []
    beats: list[Any] = []
    d = AlarmDaemon(reader=lambda: feed[0], player=Broken(), cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: clock[0], publish_audio_failed=_recorder(published),
                    heartbeat=_recorder(beats))
    d.run_once()
    clock[0] = at(1)
    d.run_once()
    assert published == [True] and not beats
    assert d.cache.load().audio_failed
    feed[0] = inp(stop_pressed_at=at(1.5))
    clock[0] = at(2)
    d.run_once()
    d.player = FakePlayer()
    feed[0] = inp(at(3))
    clock[0] = at(3)
    d.run_once()
    assert published == [True, False]


def test_pwplay_failure_restores_partial_start(tmp_path: Path) -> None:
    import subprocess
    calls: list[list[str]] = []
    dump = json.dumps([{"id": 52, "info": {"props": {"media.class": "Audio/Sink", "node.name": "analog"}}}])
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=dump if args[0] == "pw-dump" else "Volume: 0.20 [MUTED]")
    proc = _Proc()
    proc.done = True
    waits: list[float] = []

    def wait(_stop: Any, delay: float) -> bool:
        waits.append(delay)
        return False

    p = PipeWirePlayer("analog", tmp_path / "tone.wav", run=run,
                       popen=lambda *a, **kw: proc, wait=wait)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="pw-play recovery exhausted"):
        p.start()
    assert waits == [0.25, 0.5, 1.0]
    assert calls[-2:] == [["wpctl", "set-volume", "52", "0.20"], ["wpctl", "set-mute", "52", "1"]]


def test_pwplay_nonzero_tick_is_not_respawned(tmp_path: Path) -> None:
    import subprocess
    class BadProc(_Proc):
        def poll(self) -> int | None:
            return 1 if self.done else None
    proc = BadProc()
    dump = json.dumps([{"id": 52, "info": {"props": {"media.class": "Audio/Sink", "node.name": "analog"}}}])
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=dump if args[0] == "pw-dump" else "Volume: 0.20")
    p = PipeWirePlayer("analog", tmp_path / "tone.wav", run=run, popen=lambda *a, **kw: proc)  # type: ignore[arg-type]
    p.start()
    proc.done = True
    assert p._worker is not None
    p._worker.join(timeout=2)
    assert not p._worker.is_alive()
    with pytest.raises(RuntimeError, match="audio worker"):
        p.tick()
    p.stop()


def test_activity_callback_only_stops_active_alarm(tmp_path: Path) -> None:
    clock = [at(-1)]
    player = FakePlayer()
    d = AlarmDaemon(reader=lambda: inp(), player=player, cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: clock[0])
    d.activity()  # idle activity ignored
    assert d._activity_pressed is None
    clock[0] = at(0)
    d.run_once()
    clock[0] = at(0.1)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and player.events[-1] == "stop"
    assert "stop_pressed" not in d.cache.path.read_text()


def test_activity_monitor_generated_config_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from commutecompass.alarm_daemon import ActivityMonitor
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: "/fake/hypridle")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    import socket
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(tmp_path / "wayland-2"))
    proc = _Proc()
    files: list[Path] = []
    def popen(args: list[str], **kw: Any) -> Any:
        files.append(Path(args[-1]))
        assert kw["env"]["WAYLAND_DISPLAY"] == "wayland-2"
        body = files[0].read_text()
        assert "timeout = 1" in body and f"kill -USR1 {os.getpid()}" in body
        return proc
    monitor = ActivityMonitor(popen=popen)
    try:
        monitor.start()
        assert files[0].exists()
        monitor.stop()
        assert proc.terminated and not files[0].exists()
    finally:
        sock.close()


def test_missing_activity_monitor_does_not_disable_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from commutecompass.alarm_daemon import ActivityMonitor
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: None)
    player = FakePlayer()
    d = AlarmDaemon(reader=lambda: inp(), player=player, cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: at(0), activity_monitor=ActivityMonitor(),
                    sleep=lambda _: d.request_stop())
    d.run_forever()
    assert player.events[0] == "start" and player.events[-1] == "stop"


@pytest.mark.parametrize("snoozed", [False, True])
def test_next_day_rollover_preserves_active(snoozed: bool) -> None:
    st, _ = step(DaemonState(), at(0), helper_inputs(), S)
    if snoozed:
        st, _ = step(st, at(1), helper_inputs(snooze_pressed_at=at(0.5)), S)
    st, acts = step(st, at(2), helper_inputs(wake=at(1440), sensor=at(1440)), S)
    assert st.mode == ("snoozed" if snoozed else "ringing")
    assert st.active == T0 and not acts
    st, acts = step(st, at(3), helper_inputs(wake=at(1440), skip=T0), S)
    assert st.mode == "idle" and "state:idle" in kinds(acts)


def test_next_day_rollover_preserves_cached_elapsed_pending() -> None:
    st = DaemonState(pending=T0, pending_kind="wake")
    st, acts = step(st, at(0.5), helper_inputs(wake=at(1440), sensor=at(1440)), S)
    assert st.active == T0 and st.mode == "ringing" and "start" in kinds(acts)


def test_restart_resumes_claimed_audio_without_extending_bound(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    # Crash between saving claim and starting audio.
    st, _ = step(DaemonState(), at(0), helper_inputs(), S)
    st = replace(st, snoozes=1)
    cache.save(st)
    clock = [at(2)]
    player = FakePlayer()
    ack: list[Any] = []
    d = AlarmDaemon(reader=lambda: helper_inputs(wake=at(1440)), player=player,
                    cache=cache, settings=S, clock=lambda: clock[0], publish_handled=_recorder(ack))
    d.run_once()
    assert player.events[0] == "start" and ack == [T0]
    assert d.state.ring_started == T0 and d.state.snoozes == 1
    clock[0] = at(15)
    d.run_once()
    assert d.state.mode == "idle" and player.events[-1] == "stop"


def test_restart_expired_ring_does_not_start_audio(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(0), inp(), S)
    cache.save(st)
    player = FakePlayer()
    ack: list[Any] = []
    d = AlarmDaemon(reader=lambda: inp(), player=player, cache=cache, settings=S,
                    clock=lambda: at(16), publish_handled=_recorder(ack))
    d.run_once()
    assert "start" not in player.events and not ack and d.state.mode == "idle"


@pytest.mark.parametrize("snoozed", [False, True])
@pytest.mark.parametrize("replacement", [at(-5), at(60), at(1440)])
def test_replacement_wake_preserves_active_legacy_alarm(
    snoozed: bool, replacement: datetime,
) -> None:
    st, _ = step(DaemonState(), T0, helper_inputs(), S)
    if snoozed:
        st, _ = step(st, at(1), helper_inputs(snooze_pressed_at=at(0.5)), S)
    original = st
    st, acts = step(st, at(2), helper_inputs(wake=replacement, sensor=replacement), S)
    assert st.mode == original.mode and acts == []
    assert st.active == T0 and st.active_kind == "wake"
    assert st.session is not None and original.session is not None
    assert st.session.id == original.session.id and st.session.original_at == T0
    assert st.session.locked_deadline_at == original.session.locked_deadline_at
    assert st.ring is not None and original.ring is not None
    assert st.ring.id == original.ring.id and st.ring.started_at == original.ring.started_at
    assert st.ring_started == original.ring_started
    assert st.snooze_until == original.snooze_until and st.snoozes == original.snoozes
    assert st.ended_wake is None
    if snoozed:
        assert st.snooze_until is not None
        st, acts = step(st, st.snooze_until, helper_inputs(wake=replacement), S)
        assert st.mode == "ringing" and st.active == T0
        assert kinds(acts) == ["start", "state:ringing"]
        assert st.session is not None and original.session is not None
        assert st.session.id == original.session.id


@pytest.mark.parametrize("snoozed", [False, True])
@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("replacement", [at(-5), at(60), at(1440)])
def test_cached_active_wake_survives_replacement_publication(
    tmp_path: Path, snoozed: bool, window: bool, replacement: datetime,
) -> None:
    from commutecompass.alarm_daemon import WindowSettings

    settings = Settings(window=WindowSettings()) if window else S
    deadline = at(37) if window else None
    original_inputs = helper_inputs(deadline_at=deadline, deadline_known=window)
    st, _ = step(DaemonState(), T0, original_inputs, settings)
    if snoozed:
        st, _ = step(st, at(1), replace(original_inputs, snooze_pressed_at=at(0.5)), settings)
    assert st.session is not None and st.ring is not None
    original = st
    cache = AlarmCache(tmp_path / "active.json")
    cache.save(st)
    # A genuine earlier commute replan may tighten D, but never postpone it.
    replacement_deadline = replacement + timedelta(minutes=37) if window else None
    feed = helper_inputs(wake=replacement, sensor=replacement,
                         deadline_at=replacement_deadline, deadline_known=window)
    player = FakePlayer()
    daemon = AlarmDaemon(lambda: feed, player, cache, settings, clock=lambda: at(2))
    daemon.run_once()
    resumed = daemon.state
    assert resumed.mode == original.mode and resumed.active == T0
    assert resumed.session is not None and resumed.session.id == st.session.id
    assert resumed.session.original_at == T0 and resumed.active_kind == "wake"
    assert resumed.ring is not None and resumed.ring.id == st.ring.id
    assert resumed.ring.started_at == st.ring.started_at
    assert resumed.ring_started == st.ring_started
    assert resumed.snooze_until == original.snooze_until
    assert resumed.snoozes == original.snoozes and resumed.phase == original.phase
    expected_deadline = min(at(37), replacement_deadline) if replacement_deadline else None
    assert resumed.deadline == expected_deadline
    assert resumed.ended_wake is None
    assert player.events == ([] if snoozed else ["start"])
    persisted = cache.load()
    assert persisted.session is not None and persisted.ring is not None
    assert persisted.session.id == resumed.session.id
    assert persisted.session.original_at == T0
    assert persisted.session.locked_deadline_at == expected_deadline
    assert persisted.ring.id == resumed.ring.id
    assert persisted.ring.started_at == resumed.ring.started_at
    assert persisted.deadline == resumed.deadline


def test_restart_failed_resume_latches_without_ack(tmp_path: Path) -> None:
    class Broken(FakePlayer):
        def start(self, profile: RingProfile | None = None) -> None:
            raise RuntimeError("audio unavailable")
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(0), inp(), S)
    cache.save(st)
    ack: list[Any] = []
    failed: list[Any] = []
    d = AlarmDaemon(reader=lambda: inp(), player=Broken(), cache=cache, settings=S,
                    clock=lambda: at(1), publish_handled=_recorder(ack),
                    publish_audio_failed=_recorder(failed))
    d.run_once()
    d.run_once()
    assert failed == [True] and not ack and cache.load().audio_failed


def test_handled_ack_only_successful_start_and_snooze_restart(tmp_path: Path) -> None:
    clock = [at(-1)]
    feed = [helper_inputs(sensor=T0)]
    ack: list[Any] = []
    d = AlarmDaemon(reader=lambda: feed[0], player=FakePlayer(), cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: clock[0], publish_handled=_recorder(ack))
    d.run_once()
    assert not ack
    clock[0] = at(0)
    d.run_once()
    assert ack == [T0]
    clock[0] = at(1)
    feed[0] = helper_inputs(snooze_pressed_at=at(0.5))
    d.run_once()
    clock[0] = at(10)
    feed[0] = helper_inputs()
    d.run_once()
    assert ack == [T0, T0]  # acknowledge original alarm, not snooze deadline


@pytest.mark.parametrize("raw", ["unknown", "unavailable", "2026-11-01 01:30:00"])
def test_authoritative_utc_unknown_never_uses_legacy_fold(raw: str) -> None:
    cfg = AlarmConfig()
    states = _states(**{cfg.wake_utc_entity: {"state": raw},
                       cfg.wake_entity: {"state": "2026-11-01 01:30:00"}})
    i = inputs_from_states(states, cfg)
    assert not i.wake_known and i.wake_at is None


def test_utc_helpers_preserve_fall_back_instants() -> None:
    from datetime import UTC
    cfg = AlarmConfig()
    states = _states(**{cfg.wake_utc_entity: {"state": "2026-11-01T06:30:00+00:00"},
                       cfg.nap_utc_entity: {"state": "2026-11-01T05:30:00Z"},
                       cfg.skipped_utc_entity: {"state": "2026-11-01T05:30:00Z"},
                       cfg.next_prep_utc_entity: {"state": "2026-11-01T07:00:00Z"}})
    i = inputs_from_states(states, cfg)
    assert i.wake_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert i.nap_at == i.skipped_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert i.prep_threshold == datetime(2026, 11, 1, 7, tzinfo=UTC)


def test_heartbeat_interval_crosses_dst_fall_back(tmp_path: Path) -> None:
    from datetime import UTC
    clock = [datetime(2026, 11, 1, 5, 59, 30, tzinfo=UTC).astimezone(NYC_TZ)]
    beats: list[Any] = []
    d = AlarmDaemon(reader=lambda: inp(None), player=FakePlayer(), cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: clock[0], heartbeat=_recorder(beats))
    d.run_once()
    clock[0] = datetime(2026, 11, 1, 6, 0, 30, tzinfo=UTC).astimezone(NYC_TZ)
    d.run_once()
    assert len(beats) == 2


def test_activity_supervision_boot_absent_then_child_death(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from commutecompass.alarm_daemon import ActivityMonitor
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: "/fake/hypridle")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    clock = [0.0]
    monkeypatch.setattr("commutecompass.alarm_daemon._time.monotonic", lambda: clock[0])
    procs: list[_Proc] = []
    def popen(*args: Any, **kw: Any) -> Any:
        proc = _Proc()
        procs.append(proc)
        return proc
    monitor = ActivityMonitor(popen=popen)
    monitor.tick()
    assert not procs
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(tmp_path / "wayland-3"))
    try:
        clock[0] = 1
        monitor.tick()
        assert not procs  # no hot-loop retry
        clock[0] = 5
        monitor.tick()
        assert len(procs) == 1
        procs[0].done = True
        clock[0] = 6
        monitor.tick()
        assert len(procs) == 1
        clock[0] = 11
        monitor.tick()
        assert len(procs) == 2
        # Socket disappears while the child is still alive: terminate it and
        # rediscover a new display rather than trusting stale environment.
        (tmp_path / "wayland-3").unlink()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-3")
        clock[0] = 12
        monitor.tick()
        assert procs[1].terminated
        sock2 = socket.socket(socket.AF_UNIX)
        sock2.bind(str(tmp_path / "wayland-4"))
        try:
            clock[0] = 17
            monitor.tick()
            assert len(procs) == 3
            monitor.stop()
            assert procs[-1].terminated
        finally:
            sock2.close()
    finally:
        sock.close()


@pytest.mark.parametrize("utc_ok,legacy_ok", [(True, True), (False, True), (True, False)])
def test_heartbeat_publishes_authoritative_utc_and_legacy_datetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, utc_ok: bool, legacy_ok: bool,
) -> None:
    from datetime import UTC
    cfg = AlarmConfig(cache_path=str(tmp_path / "c.json"), sound=str(tmp_path / "tone.wav"),
                      stop_on_activity=False)
    assert cfg.heartbeat_utc_entity == "input_text.commute_alarm_heartbeat_utc"
    config = Config.model_construct(
        alarm=cfg, home_assistant=HomeAssistantConfig(enabled=True, base_url="http://ha"),
        home_assistant_token="test-token",
    )
    calls: list[Any] = []
    def service(base: str, token: str, domain: str, action: str, data: dict[str, Any]) -> bool:
        calls.append((domain, action, data))
        return utc_ok if domain == "input_text" else legacy_ok
    monkeypatch.setattr("commutecompass.ha_client.call_service", service)
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: "/fake/tool")
    daemon = build_daemon(config)
    # Both occurrences of NYC's repeated 01:30 must be distinct to HA.
    for hour in (5, 6):
        now = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
        assert daemon.heartbeat(now.astimezone(NYC_TZ)) is utc_ok
        assert calls[-2:] == [
            ("input_text", "set_value", {"entity_id": cfg.heartbeat_utc_entity,
                                         "value": now.isoformat()}),
            ("input_datetime", "set_datetime", {"entity_id": cfg.heartbeat_entity,
                                                "timestamp": now.timestamp()}),
        ]


ZERO = "1970-01-01T00:00:00+00:00"


def test_nap_snooze_shutdown_restart_preserves_ha_cleanup_mode(tmp_path: Path) -> None:
    cfg = AlarmConfig()
    states = {
        cfg.nap_utc_entity: {"state": T0.isoformat()},
        cfg.wake_utc_entity: {"state": ZERO},
        cfg.next_prep_utc_entity: {"state": ZERO},
        cfg.enabled_entity: {"state": "on"},
    }
    clock = [at(0)]
    published: list[Any] = []
    cache = AlarmCache(tmp_path / "c.json")
    first = AlarmDaemon(reader=lambda: inputs_from_states(states, cfg), player=FakePlayer(),
                        cache=cache, settings=S, clock=lambda: clock[0],
                        publish_state=_recorder(published))
    first.run_once()
    clock[0] = at(14)
    states[cfg.snooze_button] = {"state": at(13.9).isoformat()}
    first.run_once()
    assert first.state.snooze_until == at(23)
    first.request_stop()
    first.run_forever()  # shutdown must not publish idle
    assert published == ["ringing", "snoozed"]
    assert cache.load().mode == "snoozed"
    clock[0] = at(16)
    player = FakePlayer()
    resumed = AlarmDaemon(reader=lambda: inputs_from_states(states, cfg), player=player,
                          cache=cache, settings=S, clock=lambda: clock[0],
                          publish_state=_recorder(published))
    resumed.run_once()
    assert published[-1] == "snoozed" and "start" not in player.events
    clock[0] = at(20)
    resumed.run_once()
    assert published[-1] == "snoozed"  # HA cleanup sees active snooze, retains nap
    clock[0] = at(23)
    resumed.run_once()
    assert resumed.state.mode == "ringing" and player.events[0] == "start"
    assert published[-1] == "ringing" and "idle" not in published


def test_failed_mode_publication_retries_current_not_stale_mode(tmp_path: Path) -> None:
    attempts: list[Any] = []
    clock = [at(0)]
    feed = [inp()]
    def publish(mode: str) -> bool:
        attempts.append(mode)
        return len(attempts) in (2, 4)
    d = AlarmDaemon(reader=lambda: feed[0], player=FakePlayer(), cache=AlarmCache(tmp_path / "c.json"),
                    settings=S, clock=lambda: clock[0], publish_state=publish)
    d.run_once()
    d.run_once()
    assert attempts == ["ringing", "ringing"]
    clock[0] = at(1)
    feed[0] = inp(snooze_pressed_at=at(0.5))
    d.run_once()
    assert attempts[-1] == "snoozed"
    feed[0] = inp(stop_pressed_at=at(0.75))
    d.run_once()
    d.run_once()
    assert attempts == ["ringing", "ringing", "snoozed", "idle"]


@pytest.mark.parametrize("raw", [None, "unknown", "unavailable", "bad", "2026-11-01 01:30:00"])
def test_unknown_utc_authority_preserves_cache_and_cannot_seed_sensor(
    raw: str | None,
) -> None:
    cfg = AlarmConfig()
    states = _states(**{cfg.wake_entity: {"state": at(60).isoformat()}})
    states.pop(cfg.wake_utc_entity)
    if raw is not None:
        states[cfg.wake_utc_entity] = {"state": raw}
    i = inputs_from_states(states, cfg)
    assert not i.wake_known
    st, acts = step(DaemonState(), at(0), i, S)
    assert st.mode == "idle" and st.pending is None and not acts
    st, acts = step(DaemonState(pending=T0, pending_kind="wake"), at(0.5), i, S)
    assert st.mode == "ringing" and "start" in kinds(acts)
    st, _ = step(st, at(1), i, S)
    assert st.mode == "ringing"
    snoozed = replace(st, mode="snoozed", snooze_until=at(9), snoozes=1)
    snoozed, acts = step(snoozed, at(2), i, S)
    assert snoozed.mode == "snoozed" and snoozed.snooze_until == at(9) and not acts


def test_explicit_zero_schedule_cancels_despite_stale_sensor() -> None:
    cfg = AlarmConfig()
    i = inputs_from_states(_states(**{cfg.wake_utc_entity: {"state": ZERO}}), cfg)
    assert i.wake_known and i.wake_at is None
    for state in (DaemonState(), DaemonState(pending=T0, pending_kind="wake")):
        st, acts = step(state, at(0.5), i, S)
        assert st.mode == "idle" and st.pending is None and not acts
    active, _ = step(DaemonState(), at(0), inp(), S)
    active, acts = step(active, at(1), i, S)
    assert active.mode == "idle" and "stop:cancelled" in kinds(acts)


@pytest.mark.parametrize("raw", [None, "unknown", "unavailable", ZERO])
def test_cap_unknown_preserves_prior_zero_clears_without_ending_snooze(raw: str | None) -> None:
    cfg = AlarmConfig()
    states = _states(**{cfg.next_prep_utc_entity: {"state": at(8).isoformat()}})
    st, _ = step(DaemonState(), at(0), inputs_from_states(states, cfg), S)
    states[cfg.snooze_button] = {"state": at(0.5).isoformat()}
    st, _ = step(st, at(1), inputs_from_states(states, cfg), S)
    assert st.mode == "snoozed" and st.snooze_until == at(8)
    states.pop(cfg.next_prep_utc_entity)
    if raw is not None:
        states[cfg.next_prep_utc_entity] = {"state": raw}
    st, acts = step(st, at(2), inputs_from_states(states, cfg), S)
    assert st.mode == "snoozed" and not acts
    assert st.prep_threshold == (None if raw == ZERO else at(8))
    # A genuine elapsed nonzero cap remains hard, unlike epoch zero.
    states[cfg.next_prep_utc_entity] = {"state": at(1.5).isoformat()}
    st, acts = step(st, at(2), inputs_from_states(states, cfg), S)
    assert st.mode == "ringing" and "start" in kinds(acts)


def test_run_daemon_does_not_publish_startup_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    from commutecompass.alarm_daemon import run_daemon
    class Stub:
        def publish_state(self, mode: str) -> bool:
            pytest.fail(f"startup must not publish {mode}")
        def request_stop(self, *args: Any) -> None:
            pass
        def activity(self, *args: Any) -> None:
            pass
        def run_forever(self) -> None:
            pass
    monkeypatch.setattr("commutecompass.alarm_daemon.build_daemon", lambda _: Stub())
    monkeypatch.setattr("commutecompass.alarm_daemon.signal.signal", lambda *args: None)
    run_daemon(Config.model_construct())
