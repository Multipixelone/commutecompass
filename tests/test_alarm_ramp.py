"""Local ramp regressions: fake time/subprocesses, never real audio or HA."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from typing import Any

import pytest
from test_alarm_daemon import T0, _Proc, _recorder, at, inp

from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    PipeWirePlayer,
    Settings,
    _default_run,
    build_daemon,
)
from commutecompass.config import AlarmConfig, Config, HomeAssistantConfig


def test_build_daemon_passes_configured_ramp_to_player(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.model_construct(
        home_assistant=HomeAssistantConfig(enabled=True, base_url="http://fake"),
        home_assistant_token="fake",
        alarm=AlarmConfig(sound=str(tmp_path / "alarm.opus"), ramp_seconds=10,
                          cache_path=str(tmp_path / "cache.json"),
                          ramp_start_percent=30, stop_on_activity=False),
    )
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: "/fake/tool")
    daemon = build_daemon(config)
    assert isinstance(daemon.player, PipeWirePlayer)
    assert daemon.player._ramp_seconds == 10
    assert daemon.player._initial_volume == 0.3


def test_resume_keeps_ring_deadline_but_starts_local_fade(tmp_path: Path) -> None:
    from commutecompass.alarm_daemon import DaemonState, step

    h = RampHarness(tmp_path / "alarm.opus")
    cache = AlarmCache(tmp_path / "cache.json")
    state, _ = step(DaemonState(), T0, inp(), Settings())
    cache.save(state)
    daemon = AlarmDaemon(
        reader=lambda: inp(), player=h.player, cache=cache, settings=Settings(),
        clock=lambda: at(1),
    )
    daemon.run_once()
    try:
        h.ready()
        assert daemon.state.ring_started == T0
        assert h.volumes == [0.3]
        h.advance(10)
        assert h.volumes[-1] == 1
    finally:
        h.player.stop()


def test_volume_subprocess_has_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    _default_run(["wpctl", "set-volume", "52", "0.5"])
    _default_run(["pw-dump"])
    assert timeouts == [0.5, 10]


class RampHarness:
    """Gate every worker wait so tests advance monotonic time without sleeping."""

    def __init__(self, path: Path, *, seconds: int = 10, start: float = 0.3) -> None:
        self.now = 100.0
        self.calls: list[tuple[float, list[str]]] = []
        self.procs: list[_Proc] = []
        self.waits: Queue[float] = Queue()
        self.resume = Event()
        self.fail_volume = False
        self.player = PipeWirePlayer(
            "analog", path, run=self.run, popen=self.popen,  # type: ignore[arg-type]
            initial_volume=start, ramp_seconds=seconds,
            monotonic=lambda: self.now, wait=self.wait,
        )

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append((self.now, args))
        out = ""
        if args[0] == "pw-dump":
            # Sink setup must not count towards the fade.
            self.now += 20
            out = json.dumps([{"id": 52, "info": {"props": {
                "media.class": "Audio/Sink", "node.name": "alsa.analog",
            }}}])
        elif args[1] == "get-volume":
            out = "Volume: 0.20 [MUTED]"
        if self.fail_volume and args[1] == "set-volume":
            self.fail_volume = False
            raise subprocess.TimeoutExpired(args, 0.5)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    def popen(self, args: list[str], **_: Any) -> _Proc:
        self.calls.append((self.now, args))
        proc = _Proc()
        self.procs.append(proc)
        return proc

    def wait(self, stop: Event, delay: float) -> bool:
        self.waits.put(delay)
        # Event.wait is replaced only at this test seam; stop must wake it.
        while not stop.is_set():
            if self.resume.wait(0.01):
                self.resume.clear()
                break
        return stop.is_set()

    def ready(self) -> float:
        return self.waits.get(timeout=2)

    def advance(self, seconds: float) -> float:
        self.now += seconds
        self.resume.set()
        return self.ready()

    @property
    def volumes(self) -> list[float]:
        return [float(args[-1]) for _, args in self.calls if args[:2] == ["wpctl", "set-volume"]]


@pytest.mark.parametrize("loop_every", [None, 0.6, 2.0])
def test_ten_second_ramp_is_gradual_and_independent_of_file_length(
    tmp_path: Path, loop_every: float | None,
) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    h.player.start()
    try:
        assert h.ready() == pytest.approx(0.05)
        assert h.volumes == [0.3]
        origin = h.now
        for i in range(1, 201):
            h.now = origin + i * 0.05
            if loop_every is not None and i % round(loop_every / 0.05) == 0:
                h.procs[-1].done = True
            h.resume.set()
            h.ready()
            assert h.volumes[-1] == pytest.approx(0.3 + 0.7 * i / 200, abs=0.0001)
        assert len(h.volumes) == 201
        assert max(b - a for a, b in zip(h.volumes, h.volumes[1:])) <= 0.0036
        assert len(h.procs) > 1 if loop_every is not None else len(h.procs) == 1
        h.advance(791)  # long-file duration must not stretch/reset the fade
        assert len(h.volumes) == 201
    finally:
        h.player.stop()
    assert h.volumes[-1] == 0.2
    assert h.calls[-1][1] == ["wpctl", "set-mute", "52", "1"]


def test_delayed_worker_skips_missed_slots_without_catchup(tmp_path: Path) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    h.player.start()
    try:
        h.ready()
        h.advance(6)
        assert h.volumes == [0.3, 0.72]
        h.advance(4)
        assert h.volumes == [0.3, 0.72, 1.0]
        h.advance(5)
        assert h.volumes == [0.3, 0.72, 1.0]
    finally:
        h.player.stop()


@pytest.mark.parametrize(("seconds", "start"), [(0, 0.3), (10, 1.0)])
def test_no_unnecessary_updates_for_immediate_or_full_volume_ramp(
    tmp_path: Path, seconds: int, start: float,
) -> None:
    h = RampHarness(tmp_path / "alarm.opus", seconds=seconds, start=start)
    h.player.start()
    try:
        h.ready()
        assert h.volumes[-1] == 1
        count = len(h.volumes)
        h.advance(1)
        assert len(h.volumes) == count
    finally:
        h.player.stop()


def test_stop_cancels_worker_before_restore_and_new_ring(tmp_path: Path) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    for _ in range(2):
        h.player.start()
        h.ready()
        assert h.volumes[-1] == 0.3
        h.advance(1)
        assert h.volumes[-1] == 0.37
        h.player.stop()
        calls = list(h.calls)
        h.now += 100
        h.player.tick()
        h.player.stop()
        assert h.calls == calls  # no obsolete worker or repeated restoration
        assert h.procs[-1].terminated
        assert h.calls[-2][1] == ["wpctl", "set-volume", "52", "0.20"]
        assert h.calls[-1][1] == ["wpctl", "set-mute", "52", "1"]


def test_worker_failure_restores_and_is_reported_on_health_check(tmp_path: Path) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    h.player.start()
    h.ready()
    h.fail_volume = True
    h.now += 1
    h.resume.set()
    # stop joins the worker, including an in-flight failing volume command.
    # Wait for termination without real-time sleeps.
    assert h.player._worker is not None
    h.player._worker.join(timeout=2)
    assert not h.player._worker.is_alive()
    with pytest.raises(RuntimeError, match="audio worker"):
        h.player.tick()
    assert h.calls[-2][1] == ["wpctl", "set-volume", "52", "0.20"]
    assert h.calls[-1][1] == ["wpctl", "set-mute", "52", "1"]
    calls = list(h.calls)
    h.player.stop()
    assert h.calls == calls


@pytest.mark.parametrize("action", ["stop", "snooze", "timeout", "shutdown", "failure"])
def test_daemon_quiesces_local_worker_on_all_exit_paths(tmp_path: Path, action: str) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    wall = [T0]
    feed = [inp()]
    failures: list[bool] = []
    beats: list[Any] = []
    daemon = AlarmDaemon(
        reader=lambda: feed[0], player=h.player, cache=AlarmCache(tmp_path / "cache.json"),
        settings=Settings(), clock=lambda: wall[0],
        publish_audio_failed=_recorder(failures),
        heartbeat=_recorder(beats),
    )
    daemon.run_once()
    h.ready()
    if action == "failure":
        h.fail_volume = True
        h.now += 1
        h.resume.set()
        assert h.player._worker is not None
        h.player._worker.join(timeout=2)
        assert not h.player._worker.is_alive()
        wall[0] = at(1)
        daemon.run_once()
        assert failures == [False, True]  # successful start clears latch, failure sets it
        assert beats == [T0]  # no healthy heartbeat after worker failure
        assert daemon.cache.load().audio_failed
    elif action == "shutdown":
        daemon.request_stop()
        daemon.run_forever()
    else:
        wall[0] = at(16 if action == "timeout" else 1)
        if action == "stop":
            feed[0] = inp(stop_pressed_at=at(0.5))
        elif action == "snooze":
            feed[0] = inp(snooze_pressed_at=at(0.5))
        daemon.run_once()
        assert daemon.state.mode == ("snoozed" if action == "snooze" else "idle")
    assert h.player._worker is None
    assert h.procs[-1].terminated
    assert h.volumes[-1] == 0.2
    calls = list(h.calls)
    h.now += 10
    h.player.stop()
    assert h.calls == calls


@pytest.mark.parametrize("command_fails", [False, True])
def test_stop_waits_only_for_inflight_command_before_restoring(
    tmp_path: Path, command_fails: bool,
) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    entered, release = Event(), Event()
    original_run = h.player._runner
    active = 0
    max_active = 0

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal active, max_active
        active += 1
        max_active = max(active, max_active)
        try:
            if args[:2] == ["wpctl", "set-volume"] and args[-1] == "0.3700":
                entered.set()
                assert release.wait(2)
                if command_fails:
                    raise subprocess.TimeoutExpired(args, 0.5)
            return original_run(args)
        finally:
            active -= 1

    original_restore = h.player._restore

    def restore() -> None:
        # Cancellation owns restoration only after the worker has been joined.
        assert h.player._worker is None
        original_restore()

    h.player._restore = restore  # type: ignore[method-assign]
    h.player._runner = run
    h.player.start()
    h.ready()
    h.now += 1
    h.resume.set()
    stopper = Thread(target=h.player.stop)
    try:
        assert entered.wait(2)
        stopper.start()
        assert h.player._stop.wait(2)
        assert stopper.is_alive()  # cannot restore concurrently with set-volume
        assert 0.2 not in h.volumes
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(timeout=2)
        h.player.stop()
    assert not stopper.is_alive()
    assert max_active == 1
    assert h.player._worker is None
    assert h.volumes == ([0.3, 0.2] if command_fails else [0.3, 0.37, 0.2])
    assert h.calls[-1][1] == ["wpctl", "set-mute", "52", "1"]
    calls = list(h.calls)
    h.now += 10
    if not command_fails:
        h.player.tick()
    assert h.calls == calls


@pytest.mark.parametrize("fault", ["terminate", "wait", "kill", "reap", "volume", "mute", None])
def test_stop_cleanup_failures_restore_and_retain_retryable_state(
    tmp_path: Path, fault: str | None,
) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    h.player.start()
    h.ready()
    events: list[str] = []
    failing = True

    class StopProc(_Proc):
        def terminate(self) -> None:
            events.append("terminate")
            if failing and fault == "terminate":
                raise OSError("terminate failed")

        def wait(self, timeout: float) -> int:
            events.append("wait")
            if not self.done:
                if failing and fault == "wait":
                    raise OSError("wait failed")
                raise subprocess.TimeoutExpired("pw-play", timeout)
            if failing and fault == "reap":
                raise OSError("reap failed")
            return 0

        def kill(self) -> None:
            events.append("kill")
            if failing and fault == "kill":
                raise OSError("kill failed")
            self.done = True

    proc = StopProc()
    # Replace the mock playback process only after launch; no real process exists.
    h.player._proc = proc  # type: ignore[assignment]
    original_run = h.player._runner

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["wpctl", "set-volume"] and args[-1] == "0.20":
            events.append("volume")
            if failing and fault == "volume":
                raise OSError("volume failed")
        if args[:2] == ["wpctl", "set-mute"] and args[-1] == "1":
            events.append("mute")
            if failing and fault == "mute":
                raise OSError("mute failed")
        return original_run(args)

    h.player._runner = run
    try:
        if fault is None:
            h.player.stop()
        else:
            with pytest.raises(OSError, match="failed"):
                h.player.stop()
        assert h.player._worker is None
        assert events[-2:] == ["volume", "mute"]
        assert "kill" in events
        if fault != "kill":
            assert events[events.index("kill") + 1] == "wait"  # reap after kill
        assert (h.player._proc is not None) == (fault in {"kill", "reap"})
        assert (h.player._saved is not None) == (fault in {"volume", "mute"})
        if fault in {"kill", "reap", "volume", "mute"}:
            count = len(h.procs)
            with pytest.raises(OSError, match="failed"):
                h.player.start()
            assert len(h.procs) == count  # never start over unresolved cleanup
            assert h.player._worker is None
    finally:
        failing = False
        h.player.stop()
    assert h.player._proc is None
    assert h.player._saved is None
    calls = list(h.calls)
    h.player.start()
    h.ready()
    assert h.volumes[-1] == 0.3
    h.player.stop()
    assert h.calls[:len(calls)] == calls


@pytest.mark.parametrize("blocking", ["read", "publish"])
def test_ramp_progresses_during_slow_ha_without_more_network_polls(
    tmp_path: Path, blocking: str,
) -> None:
    h = RampHarness(tmp_path / "alarm.opus")
    entered, release = Event(), Event()
    reads = 0
    publishes = 0
    wall = [T0]

    def reader() -> Any:
        nonlocal reads
        reads += 1
        if blocking == "read" and reads == 2:
            entered.set()
            assert release.wait(2)
        return inp()

    def publish(_: Any) -> bool:
        nonlocal publishes
        publishes += 1
        if blocking == "publish":
            entered.set()
            assert release.wait(2)
        return True

    daemon = AlarmDaemon(
        reader=reader, player=h.player, cache=AlarmCache(tmp_path / "cache.json"),
        settings=Settings(), clock=lambda: wall[0], publish_handled=publish,
    )
    errors: list[BaseException] = []

    def poll() -> None:
        try:
            daemon.run_once()
        except Exception as exc:  # noqa: BLE001 — surface any poll-thread failure to the test
            errors.append(exc)

    if blocking == "read":
        daemon.run_once()
        h.ready()
    thread = Thread(target=poll)
    thread.start()
    try:
        assert entered.wait(2)
        if blocking == "publish":
            h.ready()
        for _ in range(10):
            wall[0] -= timedelta(hours=1)  # wall-clock jumps cannot alter fade
            h.advance(1)
        assert h.volumes[-1] == 1
        assert reads == (2 if blocking == "read" else 1)
        assert publishes == 1
    finally:
        release.set()
        thread.join(timeout=2)
        h.player.stop()
    assert not thread.is_alive()
    assert not errors
