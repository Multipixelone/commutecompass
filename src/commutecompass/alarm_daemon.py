"""``commutecompass alarm daemon`` — ring link's speakers at HA's next alarm.

Split into independently testable pieces (see ``docs/alarm-design.md``):

* :class:`HAReader` — one ``GET /api/states`` per poll → :class:`Inputs`.
* :class:`AlarmCache` — local JSON so a HA outage can't silence a known alarm.
* :func:`step` — pure state machine ``(state, now, inputs) → (state, actions)``.
* :class:`AudioPlayer` protocol + :class:`PipeWirePlayer` (wpctl / pw-play).
* :class:`AlarmDaemon` — the loop gluing them together (heartbeat, state
  write-back, ramp, SIGTERM restore).

State machine::

    idle    → ringing   now ≥ alarm instant, not already rung, ≤ 10 min stale
    ringing → idle      stop pressed after ring start | max_ring_minutes elapsed
    ringing → snoozed   snooze pressed after ring start & snoozes < max
    snoozed → ringing   snooze elapsed
    snoozed → idle      stop pressed
    *       → idle      master switch off
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import time as _time
import wave
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Protocol, Union

import httpx

from commutecompass.timeutil import now_nyc, to_nyc

if TYPE_CHECKING:
    from commutecompass.config import AlarmConfig, Config

logger = logging.getLogger(__name__)

# Missed alarm > late alarm: never ring an instant older than this.
STALE_LIMIT = timedelta(minutes=10)
# Poll cadence while ringing/snoozed so a button press stops audio quickly.
RINGING_POLL_SECONDS = 2.0
# How many rung instants to remember (dedupe across restarts).
_DONE_MAX = 32
_UNKNOWN_STATES = frozenset({"", "unknown", "unavailable", "none", "null"})

Mode = Literal["idle", "ringing", "snoozed"]


# ─────────── Inputs from HA ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Inputs:
    """One poll's worth of HA state.  ``ha_ok=False`` → everything else unknown."""

    ha_ok: bool
    next_alarm: Optional[datetime] = None
    kind: str = ""
    enabled: bool = True
    stop_pressed_at: Optional[datetime] = None
    snooze_pressed_at: Optional[datetime] = None


class HAUnavailable(Exception):
    """HA could not be reached or returned garbage."""


def parse_ha_datetime(raw: object) -> Optional[datetime]:
    """Parse an HA entity state as a tz-aware NYC datetime (None if unknown)."""
    if not isinstance(raw, str) or raw.strip().lower() in _UNKNOWN_STATES:
        return None
    try:
        return to_nyc(datetime.fromisoformat(raw.strip()))
    except ValueError:
        return None


def fetch_states(base_url: str, token: str, *, timeout: float = 5.0) -> dict[str, dict[str, Any]]:
    """``GET /api/states`` → ``{entity_id: state_obj}``.  Raises :class:`HAUnavailable`."""
    if not (base_url and token):
        raise HAUnavailable("HA base_url/token not configured")
    url = f"{base_url.rstrip('/')}/api/states"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HAUnavailable(f"HA unreachable: {exc}") from exc
    if response.status_code != 200:
        raise HAUnavailable(f"HA /api/states returned {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HAUnavailable(f"HA /api/states bad JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise HAUnavailable("HA /api/states: expected a list")
    out: dict[str, dict[str, Any]] = {}
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str):
            out[item["entity_id"]] = item
    return out


def inputs_from_states(states: dict[str, dict[str, Any]], cfg: "AlarmConfig") -> Inputs:
    """Map the raw HA state dump onto :class:`Inputs`."""
    nxt = states.get(cfg.next_alarm_entity, {})
    attrs = nxt.get("attributes") if isinstance(nxt.get("attributes"), dict) else {}
    kind = attrs.get("kind") if isinstance(attrs, dict) else ""
    enabled_state = states.get(cfg.enabled_entity, {}).get("state")
    return Inputs(
        ha_ok=True,
        next_alarm=parse_ha_datetime(nxt.get("state")),
        kind=kind if isinstance(kind, str) else "",
        # Missing master switch entity ≠ "off": only an explicit "off" disables.
        enabled=enabled_state != "off",
        stop_pressed_at=parse_ha_datetime(states.get(cfg.stop_button, {}).get("state")),
        snooze_pressed_at=parse_ha_datetime(states.get(cfg.snooze_button, {}).get("state")),
    )


class HAReader:
    """Reads :class:`Inputs` from HA; never raises (returns ``ha_ok=False``)."""

    def __init__(self, base_url: str, token: str, cfg: "AlarmConfig") -> None:
        self._base_url = base_url
        self._token = token
        self._cfg = cfg

    def read(self) -> Inputs:
        try:
            return inputs_from_states(fetch_states(self._base_url, self._token), self._cfg)
        except HAUnavailable as exc:
            logger.warning("alarm daemon: %s — using cached alarm", exc)
            return Inputs(ha_ok=False)


# ─────────── State + cache ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DaemonState:
    mode: Mode = "idle"
    # Latest known alarm instant (from HA, or the cache while HA is down).
    pending: Optional[datetime] = None
    pending_kind: str = ""
    # Instant currently being serviced (ringing/snoozed).
    active: Optional[datetime] = None
    ring_started: Optional[datetime] = None
    snooze_until: Optional[datetime] = None
    snoozes: int = 0
    # ISO-UTC instants already rung (or skipped as stale) — never re-ring.
    done: tuple[str, ...] = ()

    def is_done(self, instant: datetime) -> bool:
        return _key(instant) in self.done


def _key(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


def _mark_done(state: DaemonState, instant: datetime) -> DaemonState:
    if state.is_done(instant):
        return state
    return replace(state, done=(*state.done, _key(instant))[-_DONE_MAX:])


class AlarmCache:
    """Local JSON persistence for ``pending`` + ``done`` (atomic replace)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> DaemonState:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return DaemonState()
        except (OSError, ValueError) as exc:
            logger.warning("alarm daemon: ignoring unreadable cache %s: %s", self.path, exc)
            return DaemonState()
        if not isinstance(raw, dict):
            return DaemonState()
        pending = parse_ha_datetime(raw.get("pending"))
        done = raw.get("done")
        return DaemonState(
            pending=pending,
            pending_kind=str(raw.get("pending_kind") or ""),
            done=tuple(str(d) for d in done)[-_DONE_MAX:] if isinstance(done, list) else (),
        )

    def save(self, state: DaemonState) -> None:
        data = {
            "pending": state.pending.isoformat() if state.pending else None,
            "pending_kind": state.pending_kind,
            "done": list(state.done),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("alarm daemon: failed to write cache %s: %s", self.path, exc)


# ─────────── Pure state machine ───────────────────────────────────────────────


@dataclass(frozen=True)
class StartRing:
    instant: datetime
    kind: str


@dataclass(frozen=True)
class StopRing:
    reason: str


@dataclass(frozen=True)
class PublishState:
    mode: Mode


Action = Union[StartRing, StopRing, PublishState]


@dataclass(frozen=True)
class Settings:
    """Subset of AlarmConfig the state machine needs."""

    max_ring: timedelta = timedelta(minutes=15)
    snooze: timedelta = timedelta(minutes=9)
    max_snoozes: int = 2
    stale_limit: timedelta = STALE_LIMIT

    @classmethod
    def from_config(cls, cfg: "AlarmConfig") -> "Settings":
        return cls(
            max_ring=timedelta(minutes=cfg.max_ring_minutes),
            snooze=timedelta(minutes=cfg.snooze_minutes),
            max_snoozes=cfg.max_snoozes,
        )


def _after(ts: Optional[datetime], ref: Optional[datetime]) -> bool:
    return ts is not None and ref is not None and ts.astimezone(UTC) > ref.astimezone(UTC)


def _le(a: datetime, b: datetime) -> bool:
    return a.astimezone(UTC) <= b.astimezone(UTC)


def step(
    state: DaemonState, now: datetime, inputs: Inputs, settings: Settings
) -> tuple[DaemonState, list[Action]]:
    """Advance the state machine by one poll."""
    actions: list[Action] = []

    # Master switch off (only trusted when HA answered).
    if inputs.ha_ok and not inputs.enabled:
        if state.mode == "ringing":
            actions.append(StopRing("disabled"))
        if state.mode != "idle":
            actions.append(PublishState("idle"))
        return replace(state, mode="idle", active=None, ring_started=None,
                       snooze_until=None, snoozes=0, pending=None, pending_kind=""), actions

    # ── Ringing / snoozed: buttons + timeouts ────────────────────────────────
    if state.mode == "ringing":
        if _after(inputs.stop_pressed_at, state.ring_started):
            actions += [StopRing("stop"), PublishState("idle")]
            state = replace(state, mode="idle", active=None, ring_started=None, snoozes=0)
        elif _after(inputs.snooze_pressed_at, state.ring_started) and state.snoozes < settings.max_snoozes:
            actions += [StopRing("snooze"), PublishState("snoozed")]
            state = replace(state, mode="snoozed", snooze_until=now + settings.snooze,
                            snoozes=state.snoozes + 1)
        elif state.ring_started is not None and _le(state.ring_started + settings.max_ring, now):
            actions += [StopRing("timeout"), PublishState("idle")]
            state = replace(state, mode="idle", active=None, ring_started=None, snoozes=0)
    elif state.mode == "snoozed":
        if _after(inputs.stop_pressed_at, state.ring_started):
            actions.append(PublishState("idle"))
            state = replace(state, mode="idle", active=None, ring_started=None,
                            snooze_until=None, snoozes=0)
        elif state.snooze_until is not None and _le(state.snooze_until, now):
            assert state.active is not None
            actions += [StartRing(state.active, state.pending_kind or "snooze"), PublishState("ringing")]
            state = replace(state, mode="ringing", ring_started=now, snooze_until=None)

    # ── Track the next alarm instant ─────────────────────────────────────────
    candidate = inputs.next_alarm
    due: Optional[datetime] = None
    due_kind = ""
    # A remembered instant that has now come due takes priority: HA's sensor
    # only shows FUTURE alarms, so it may already have moved past it.
    if state.pending is not None and _le(state.pending, now) and not state.is_done(state.pending):
        due, due_kind = state.pending, state.pending_kind
    elif candidate is not None and _le(candidate, now) and not state.is_done(candidate):
        due, due_kind = candidate, inputs.kind

    if candidate is not None and not _le(candidate, now):
        state = replace(state, pending=candidate, pending_kind=inputs.kind)
    elif inputs.ha_ok and candidate is None and (state.pending is None or not _le(state.pending, now)):
        # Upcoming alarm withdrawn (skipped / cleared nap).
        state = replace(state, pending=None, pending_kind="")

    if due is not None:
        state = _mark_done(state, due)
        if now.astimezone(UTC) - due.astimezone(UTC) > settings.stale_limit:
            logger.warning("alarm daemon: skipping stale alarm %s (now %s)", due.isoformat(), now.isoformat())
        elif state.mode == "idle":
            actions += [StartRing(due, due_kind), PublishState("ringing")]
            state = replace(state, mode="ringing", active=due, ring_started=now, snoozes=0)
        # else: already ringing/snoozed for an earlier instant → absorb this one.

    if state.pending is not None and state.is_done(state.pending):
        state = replace(state, pending=None, pending_kind="")

    return state, actions


def ramp_fraction(elapsed: timedelta, start_percent: int, ramp_seconds: int) -> float:
    """Volume 0..1 for ``elapsed`` into a ring: linear start% → 100 %."""
    start = max(0.0, min(1.0, start_percent / 100.0))
    if ramp_seconds <= 0:
        return 1.0
    t = max(0.0, min(1.0, elapsed.total_seconds() / ramp_seconds))
    return start + (1.0 - start) * t


# ─────────── Audio ────────────────────────────────────────────────────────────


class AudioPlayer(Protocol):
    def start(self) -> None:
        """Begin (looping) playback; must save prior volume/mute first."""

    def tick(self, volume: float) -> None:
        """Set volume (0..1) and restart the loop if playback ended."""

    def stop(self) -> None:
        """Stop playback and restore saved volume/mute.  Idempotent."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_run(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)


def find_sink(pw_dump: object, needle: str) -> Optional[tuple[int, str]]:
    """Return (node id, node.name) of the first Audio/Sink matching ``needle``."""
    if not isinstance(pw_dump, list):
        return None
    n = needle.lower()
    for obj in pw_dump:
        if not isinstance(obj, dict):
            continue
        info = obj.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict) or props.get("media.class") != "Audio/Sink":
            continue
        name = str(props.get("node.name") or "")
        desc = str(props.get("node.description") or props.get("node.nick") or "")
        if n in name.lower() or n in desc.lower():
            node_id = obj.get("id")
            if isinstance(node_id, int):
                return node_id, name
    return None


_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)(\s*\[MUTED\])?")


def parse_wpctl_volume(out: str) -> tuple[float, bool]:
    m = _VOLUME_RE.search(out)
    if not m:
        raise ValueError(f"unparseable wpctl volume: {out!r}")
    return float(m.group(1)), bool(m.group(2))


class PipeWirePlayer:
    """Loop ``pw-play`` on a sink resolved by substring; ramp via ``wpctl``."""

    def __init__(
        self,
        sink: str,
        sound: Path,
        *,
        run: Runner = _default_run,
        popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    ) -> None:
        self._sink = sink
        self._sound = sound
        self._run = run
        self._popen = popen
        self._node: Optional[tuple[int, str]] = None
        self._saved: Optional[tuple[float, bool]] = None
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._volume: Optional[float] = None

    def _resolve(self) -> Optional[tuple[int, str]]:
        res = self._run(["pw-dump"])
        try:
            node = find_sink(json.loads(res.stdout or "[]"), self._sink)
        except ValueError:
            node = None
        if node is None:
            logger.error("alarm daemon: no PipeWire sink matching %r — using default", self._sink)
        return node

    def _target(self) -> str:
        return str(self._node[0]) if self._node else "@DEFAULT_AUDIO_SINK@"

    def _spawn(self) -> None:
        args = ["pw-play"]
        if self._node:
            args += ["--target", self._node[1]]
        args.append(str(self._sound))
        self._proc = self._popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self) -> None:
        self.stop()
        self._node = self._resolve()
        target = self._target()
        try:
            self._saved = parse_wpctl_volume(self._run(["wpctl", "get-volume", target]).stdout or "")
        except ValueError as exc:
            logger.warning("alarm daemon: could not save volume: %s", exc)
            self._saved = None
        self._run(["wpctl", "set-mute", target, "0"])
        self._volume = None
        self._spawn()

    def tick(self, volume: float) -> None:
        if self._proc is None:
            return
        v = round(max(0.0, min(1.0, volume)), 2)
        if v != self._volume:
            self._run(["wpctl", "set-volume", self._target(), f"{v:.2f}"])
            self._volume = v
        if self._proc.poll() is not None:
            self._spawn()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._saved is not None:
            vol, muted = self._saved
            target = self._target()
            self._run(["wpctl", "set-volume", target, f"{vol:.2f}"])
            self._run(["wpctl", "set-mute", target, "1" if muted else "0"])
            self._saved = None


def synthesize_tone(path: Path, *, rate: int = 22_050) -> Path:
    """Write a ~2 s beep-beep-beep WAV (880 Hz) if ``path`` doesn't exist."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()

    def tone(seconds: float, freq: float) -> None:
        n = int(rate * seconds)
        for i in range(n):
            # Short fade in/out per beep avoids clicks.
            env = min(1.0, i / (rate * 0.005), (n - i) / (rate * 0.005))
            sample = 0.6 * env * math.sin(2 * math.pi * freq * i / rate) if freq else 0.0
            frames.extend(struct.pack("<h", int(sample * 32767)))

    for _ in range(4):
        tone(0.15, 880.0)
        tone(0.08, 0.0)
    tone(0.9, 0.0)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    os.replace(tmp, path)
    return path


# ─────────── Daemon loop ──────────────────────────────────────────────────────


def default_cache_path(config: "Config") -> Path:
    if config.alarm.cache_path:
        return Path(config.alarm.cache_path)
    return Path(config.paths.db_path).parent / "alarm-cache.json"


@dataclass
class AlarmDaemon:
    reader: Callable[[], Inputs]
    player: AudioPlayer
    cache: AlarmCache
    settings: Settings
    ramp_start_percent: int = 30
    ramp_seconds: int = 120
    poll_seconds: float = 30.0
    # Side-effect callbacks to HA (best-effort, return success).
    publish_state: Callable[[Mode], bool] = lambda _m: True
    heartbeat: Callable[[datetime], bool] = lambda _t: True
    clock: Callable[[], datetime] = now_nyc
    sleep: Callable[[float], None] = _time.sleep
    state: DaemonState = field(default_factory=DaemonState)
    _running: bool = True
    _last_heartbeat: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.state = self.cache.load()

    def run_once(self) -> float:
        """One poll; returns seconds to sleep before the next."""
        now = self.clock()
        inputs = self.reader()
        before = self.state
        self.state, actions = step(self.state, now, inputs, self.settings)
        for action in actions:
            self._apply(action)
        if self.state.mode == "ringing" and self.state.ring_started is not None:
            self.player.tick(ramp_fraction(now - self.state.ring_started,
                                           self.ramp_start_percent, self.ramp_seconds))
        if (before.pending, before.pending_kind, before.done) != (
            self.state.pending, self.state.pending_kind, self.state.done
        ):
            self.cache.save(self.state)
        if inputs.ha_ok and (
            self._last_heartbeat is None
            or (now - self._last_heartbeat).total_seconds() >= self.poll_seconds
        ):
            if self.heartbeat(now):
                self._last_heartbeat = now
        return RINGING_POLL_SECONDS if self.state.mode != "idle" else self.poll_seconds

    def _apply(self, action: Action) -> None:
        if isinstance(action, StartRing):
            logger.info("alarm daemon: ringing for %s alarm at %s", action.kind or "?", action.instant.isoformat())
            try:
                self.player.start()
            except Exception as exc:  # noqa: BLE001 — keep polling; HA fallback alerts
                logger.error("alarm daemon: audio start failed: %s", exc)
        elif isinstance(action, StopRing):
            logger.info("alarm daemon: stopping (%s)", action.reason)
            self._stop_audio()
        else:
            if not self.publish_state(action.mode):
                logger.warning("alarm daemon: failed to publish state %s", action.mode)

    def _stop_audio(self) -> None:
        try:
            self.player.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("alarm daemon: audio stop failed: %s", exc)

    def request_stop(self, *_: object) -> None:
        self._running = False

    def run_forever(self) -> None:
        try:
            while self._running:
                delay = self.run_once()
                # Sleep in short slices so SIGTERM is honoured promptly.
                remaining = delay
                while self._running and remaining > 0:
                    chunk = min(1.0, remaining)
                    self.sleep(chunk)
                    remaining -= chunk
        finally:
            self._stop_audio()
            if self.state.mode != "idle":
                self.publish_state("idle")


def build_daemon(config: "Config") -> AlarmDaemon:
    """Wire the real HA reader, PipeWire player and HA write-back."""
    from commutecompass.alarm import require_ha
    from commutecompass.ha_client import call_service

    cfg = config.alarm
    base_url, token = require_ha(config)
    cache_path = default_cache_path(config)
    sound = Path(cfg.sound) if cfg.sound else synthesize_tone(cache_path.parent / "alarm-tone.wav")
    for tool in ("wpctl", "pw-play", "pw-dump"):
        if shutil.which(tool) is None:
            logger.warning("alarm daemon: %s not on PATH — audio will fail", tool)

    def publish_state(mode: Mode) -> bool:
        return call_service(base_url, token, "input_select", "select_option",
                            {"entity_id": cfg.state_entity, "option": mode})

    def heartbeat(now: datetime) -> bool:
        return call_service(base_url, token, "input_datetime", "set_datetime",
                            {"entity_id": cfg.heartbeat_entity, "timestamp": now.timestamp()})

    return AlarmDaemon(
        reader=HAReader(base_url, token, cfg).read,
        player=PipeWirePlayer(cfg.sink, sound),
        cache=AlarmCache(cache_path),
        settings=Settings.from_config(cfg),
        ramp_start_percent=cfg.ramp_start_percent,
        ramp_seconds=cfg.ramp_seconds,
        poll_seconds=float(cfg.poll_seconds),
        publish_state=publish_state,
        heartbeat=heartbeat,
    )


def run_daemon(config: "Config") -> None:
    """Entry point for ``alarm daemon``: install SIGTERM/SIGINT handlers and loop."""
    daemon = build_daemon(config)

    def _handler(signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("alarm daemon: signal %d — restoring audio and exiting", signum)
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    daemon.publish_state("idle")
    daemon.run_forever()
