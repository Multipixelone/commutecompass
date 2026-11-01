"""Tests for alarm_daemon.py — state machine, cache, HA parsing, fake audio."""

from __future__ import annotations

import json
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

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
    Settings,
    StartRing,
    StopRing,
    find_sink,
    inputs_from_states,
    parse_wpctl_volume,
    ramp_fraction,
    step,
    synthesize_tone,
)
from commutecompass.config import AlarmConfig
from commutecompass.timeutil import NYC_TZ

T0 = datetime(2026, 10, 15, 7, 0, tzinfo=NYC_TZ)
S = Settings()


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def inp(alarm: Optional[datetime] = T0, **kw: Any) -> Inputs:
    return Inputs(ha_ok=True, next_alarm=alarm, kind="wake", **kw)


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
    st, acts = step(st, at(0.5), inp(at(600)), S)
    assert st.mode == "ringing"
    assert st.active == T0
    assert st.pending == at(600)


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
    st, acts = step(cached, at(9), Inputs(ha_ok=False), S)
    assert st.mode == "ringing"


def test_cache_fallback_when_ha_down(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    st, _ = step(DaemonState(), at(-60), inp(), S)
    cache.save(st)
    # Restart with HA unreachable.
    st2 = cache.load()
    assert st2.pending == T0
    st2, acts = step(st2, at(-1), Inputs(ha_ok=False), S)
    assert st2.mode == "idle" and st2.pending == T0
    st2, acts = step(st2, at(0), Inputs(ha_ok=False), S)
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
        def __enter__(self) -> "Boom":
            return self
        def __exit__(self, *_: Any) -> None: ...
        def get(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("down")

    monkeypatch.setattr("commutecompass.alarm_daemon.httpx.Client", Boom)
    assert HAReader("http://ha", "tok", AlarmConfig()).read() == Inputs(ha_ok=False)


def test_ha_reader_parses_list(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"entity_id": k, **v} for k, v in _states().items()]

    class Ok:
        def __init__(self, **_: Any) -> None: ...
        def __enter__(self) -> "Ok":
            return self
        def __exit__(self, *_: Any) -> None: ...
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

    def poll(self) -> Optional[int]:
        return 0 if self.done else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
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
    p.tick(0.3)
    assert ["wpctl", "set-volume", "52", "0.30"] in calls
    procs[0].done = True
    p.tick(0.5)
    assert len(procs) == 2  # looped
    p.stop()
    assert procs[1].terminated
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

    def start(self) -> None:
        self.events.append("start")

    def tick(self, volume: float) -> None:
        self.events.append(round(volume, 2))

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
    assert player.events == ["start", 0.3]
    clock[0] = at(1)
    d.run_once()
    assert player.events[-1] == pytest.approx(0.65)
    feed[0] = inp(stop_pressed_at=at(0.9))
    clock[0] = at(1.1)
    assert d.run_once() == 30.0
    assert player.events[-1] == "stop"
    assert published == ["ringing", "idle"]
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
    assert published[-1] == "idle"


def test_audio_failure_suppresses_healthy_heartbeat(tmp_path: Path) -> None:
    class BrokenPlayer(FakePlayer):
        def start(self) -> None:
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
