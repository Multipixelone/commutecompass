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
import tempfile
import time as _time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Literal, Protocol

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
    next_alarm: datetime | None = None
    kind: str = ""
    # None means unavailable: preserve the last known master switch.
    enabled: bool | None = None
    stop_pressed_at: datetime | None = None
    snooze_pressed_at: datetime | None = None
    wake_at: datetime | None = None
    nap_at: datetime | None = None
    skipped_at: datetime | None = None
    # Known helpers (including explicit cleared states) distinguish cancelled
    # schedules from the future-only sensor simply moving past its deadline.
    wake_known: bool = False
    nap_known: bool = False
    prep_threshold: datetime | None = None
    prep_known: bool = False


class HAUnavailable(Exception):
    """HA could not be reached or returned garbage."""


def parse_ha_datetime(raw: object) -> datetime | None:
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


def _helper_time(
    states: dict[str, dict[str, Any]], utc_entity: str,
) -> tuple[datetime | None, bool]:
    """UTC authority: (instant, known), with epoch zero an explicit clear.

    Missing, unavailable, malformed or offset-free text is unknown, never a
    cancellation. Legacy local helpers and the future-only sensor cannot
    replace this authority (especially during NYC's repeated DST hour).
    """
    raw = states.get(utc_entity, {}).get("state")
    if not isinstance(raw, str):
        return None, False
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        return None, False
    if instant.tzinfo is None:
        return None, False
    instant = instant.astimezone(UTC)
    return (None if instant.timestamp() == 0 else instant), True


def inputs_from_states(states: dict[str, dict[str, Any]], cfg: AlarmConfig) -> Inputs:
    """Map the raw HA state dump onto :class:`Inputs`."""
    nxt = states.get(cfg.next_alarm_entity, {})
    attrs = nxt.get("attributes") if isinstance(nxt.get("attributes"), dict) else {}
    kind = attrs.get("kind") if isinstance(attrs, dict) else ""
    enabled_state = states.get(cfg.enabled_entity, {}).get("state")
    # Publisher already subtracts wake_lead_minutes: this helper is HA's
    # effective hard cap, not the raw prep time. Never subtract the lead twice.
    threshold, prep_known = _helper_time(states, cfg.next_prep_utc_entity)
    wake, wake_known = _helper_time(states, cfg.wake_utc_entity)
    nap, nap_known = _helper_time(states, cfg.nap_utc_entity)
    skipped, _ = _helper_time(states, cfg.skipped_utc_entity)
    return Inputs(
        ha_ok=True,
        next_alarm=parse_ha_datetime(nxt.get("state")),
        kind=kind if isinstance(kind, str) else "",
        enabled={"on": True, "off": False}.get(str(enabled_state)),
        wake_at=wake,
        nap_at=nap,
        skipped_at=skipped,
        wake_known=wake_known,
        nap_known=nap_known,
        prep_threshold=threshold,
        prep_known=prep_known,
        stop_pressed_at=parse_ha_datetime(states.get(cfg.stop_button, {}).get("state")),
        snooze_pressed_at=parse_ha_datetime(states.get(cfg.snooze_button, {}).get("state")),
    )


class HAReader:
    """Reads :class:`Inputs` from HA; never raises (returns ``ha_ok=False``)."""

    def __init__(self, base_url: str, token: str, cfg: AlarmConfig) -> None:
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
    enabled: bool = True
    active_kind: str = ""
    audio_failed: bool = False
    prep_threshold: datetime | None = None
    # Latest known alarm instant (from HA, or the cache while HA is down).
    pending: datetime | None = None
    pending_kind: str = ""
    # Instant currently being serviced (ringing/snoozed).
    active: datetime | None = None
    ring_started: datetime | None = None
    snooze_until: datetime | None = None
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
    """Atomic schedule, enabled latch, consumed instants and snooze persistence."""

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
        mode = raw.get("mode")
        active = parse_ha_datetime(raw.get("active"))
        snooze_until = parse_ha_datetime(raw.get("snooze_until"))
        ring_started = parse_ha_datetime(raw.get("ring_started"))
        restored_mode: Mode = "idle"
        if active is not None:
            if mode == "snoozed" and snooze_until is not None:
                restored_mode = "snoozed"
            elif mode == "ringing" and ring_started is not None:
                restored_mode = "ringing"
        snoozes = raw.get("snoozes", 0)
        return DaemonState(
            mode=restored_mode,
            enabled=raw.get("enabled", True) is not False,
            active=active if restored_mode != "idle" else None,
            active_kind=str(raw.get("active_kind") or ""),
            ring_started=ring_started,
            snooze_until=snooze_until,
            snoozes=max(0, snoozes) if isinstance(snoozes, int) else 0,
            audio_failed=raw.get("audio_failed") is True,
            prep_threshold=parse_ha_datetime(raw.get("prep_threshold")),
            pending=pending,
            pending_kind=str(raw.get("pending_kind") or ""),
            done=tuple(str(d) for d in done)[-_DONE_MAX:] if isinstance(done, list) else (),
        )

    def save(self, state: DaemonState) -> None:
        data = {
            "pending": state.pending.isoformat() if state.pending else None,
            "pending_kind": state.pending_kind,
            "done": list(state.done),
            "mode": state.mode,
            "enabled": state.enabled,
            "active": state.active.isoformat() if state.active else None,
            "active_kind": state.active_kind,
            "ring_started": state.ring_started.isoformat() if state.ring_started else None,
            "snooze_until": state.snooze_until.isoformat() if state.snooze_until else None,
            "snoozes": state.snoozes,
            "audio_failed": state.audio_failed,
            "prep_threshold": state.prep_threshold.isoformat() if state.prep_threshold else None,
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


Action = StartRing | StopRing | PublishState


@dataclass(frozen=True)
class Settings:
    """Subset of AlarmConfig the state machine needs."""

    max_ring: timedelta = timedelta(minutes=15)
    snooze: timedelta = timedelta(minutes=9)
    max_snoozes: int = 2
    stale_limit: timedelta = STALE_LIMIT

    @classmethod
    def from_config(cls, cfg: AlarmConfig) -> Settings:
        return cls(
            max_ring=timedelta(minutes=cfg.max_ring_minutes),
            snooze=timedelta(minutes=cfg.snooze_minutes),
            max_snoozes=cfg.max_snoozes,
        )


def _after(ts: datetime | None, ref: datetime | None) -> bool:
    return ts is not None and ref is not None and ts.astimezone(UTC) > ref.astimezone(UTC)


def _le(a: datetime, b: datetime) -> bool:
    return a.astimezone(UTC) <= b.astimezone(UTC)


def _cancelled(
    instant: datetime, kind: str, inputs: Inputs, *, preserve_rollover: bool = False,
) -> bool:
    """Backing helpers, not the future-only sensor, authoritatively cancel."""
    if not inputs.ha_ok:
        return False
    if inputs.skipped_at is not None and _key(instant) == _key(inputs.skipped_at):
        return True
    known = inputs.nap_known if kind == "nap" else inputs.wake_known
    helper = inputs.nap_at if kind == "nap" else inputs.wake_at
    # Publishing tomorrow is housekeeping, not cancellation of today's due
    # or active alarm. Same-day reschedules and clears remain explicit.
    if (
        preserve_rollover
        and kind == "wake"
        and helper is not None
        and to_nyc(helper).date() > to_nyc(instant).date()
    ):
        return False
    return known and (helper is None or _key(helper) != _key(instant))


def _idle(state: DaemonState) -> DaemonState:
    return replace(state, mode="idle", active=None, active_kind="", ring_started=None,
                   snooze_until=None, snoozes=0)


def step(
    state: DaemonState, now: datetime, inputs: Inputs, settings: Settings
) -> tuple[DaemonState, list[Action]]:
    """Advance one poll; HA's future-only sensor is a hint, helpers are authority."""
    actions: list[Action] = []
    if inputs.ha_ok:
        if inputs.enabled is not None:
            state = replace(state, enabled=inputs.enabled)
        if inputs.prep_known:
            state = replace(state, prep_threshold=inputs.prep_threshold)
    if not state.enabled:
        if state.mode == "ringing":
            actions.append(StopRing("disabled"))
        if state.mode != "idle":
            actions.append(PublishState("idle"))
        return replace(_idle(state), pending=None, pending_kind=""), actions

    # Explicit skips, clears and reschedules cancel active as well as pending.
    if state.active is not None and _cancelled(state.active, state.active_kind, inputs, preserve_rollover=True):
        if state.mode == "ringing":
            actions.append(StopRing("cancelled"))
        if state.mode != "idle":
            actions.append(PublishState("idle"))
        state = _idle(state)
    if state.pending is not None and _cancelled(
        state.pending, state.pending_kind, inputs,
        preserve_rollover=_le(state.pending, now) and (
            now.astimezone(UTC) - state.pending.astimezone(UTC) <= settings.stale_limit
        ),
    ):
        state = _mark_done(state, state.pending)
        state = replace(state, pending=None, pending_kind="")

    if state.mode == "ringing":
        if _after(inputs.stop_pressed_at, state.ring_started):
            actions += [StopRing("stop"), PublishState("idle")]
            state = _idle(state)
        elif _after(inputs.snooze_pressed_at, state.ring_started) and state.snoozes < settings.max_snoozes:
            deadline = (now.astimezone(UTC) + settings.snooze).astimezone(now.tzinfo)
            if state.prep_threshold is not None and _le(state.prep_threshold, deadline):
                deadline = state.prep_threshold
            # At/past prep threshold, snooze must not silence an actionable alarm.
            if not _le(deadline, now):
                actions += [StopRing("snooze"), PublishState("snoozed")]
                state = replace(state, mode="snoozed", snooze_until=deadline,
                                snoozes=state.snoozes + 1)
        if state.mode == "ringing" and state.ring_started is not None and (
            now.astimezone(UTC) - state.ring_started.astimezone(UTC) >= settings.max_ring
        ):
            actions += [StopRing("timeout"), PublishState("idle")]
            state = _idle(state)
    elif state.mode == "snoozed":
        if _after(inputs.stop_pressed_at, state.ring_started):
            actions.append(PublishState("idle"))
            state = _idle(state)
        else:
            snooze_deadline = state.snooze_until
            if state.prep_threshold is not None and snooze_deadline is not None and _le(state.prep_threshold, snooze_deadline):
                snooze_deadline = state.prep_threshold
                state = replace(state, snooze_until=snooze_deadline)
            if snooze_deadline is not None and _le(snooze_deadline, now):
                active = state.active
                assert active is not None
                if now.astimezone(UTC) - snooze_deadline.astimezone(UTC) > settings.stale_limit:
                    state = _idle(state)
                    actions.append(PublishState("idle"))
                else:
                    actions += [StartRing(active, state.active_kind), PublishState("ringing")]
                    state = replace(state, mode="ringing", ring_started=now, snooze_until=None)

    # Recover an elapsed instant on startup even when the sensor says unknown
    # or has selected tomorrow. Backing helpers retain the elapsed wake/nap.
    candidates: list[tuple[datetime, str]] = []
    if state.pending is not None:
        candidates.append((state.pending, state.pending_kind))
    if inputs.ha_ok:
        for instant, kind, known in ((inputs.wake_at, "wake", inputs.wake_known),
                                     (inputs.nap_at, "nap", inputs.nap_known)):
            if known and instant is not None and not _cancelled(instant, kind, inputs):
                candidates.append((instant, kind))
    candidates = [(t, k) for t, k in candidates if not state.is_done(t)]
    due = sorted(((t, k) for t, k in candidates if _le(t, now)), key=lambda pair: _key(pair[0]))
    future = sorted(((t, k) for t, k in candidates if not _le(t, now)), key=lambda pair: _key(pair[0]))

    # The sensor cannot add/withdraw alarms: only UTC helpers supply new
    # deadlines. Unknown authority preserves existing cached candidates.
    pending, kind = future[0] if future else (None, "")
    state = replace(state, pending=pending, pending_kind=kind)
    for instant, kind in due:
        state = _mark_done(state, instant)
        if now.astimezone(UTC) - instant.astimezone(UTC) > settings.stale_limit:
            logger.warning("alarm daemon: skipping stale alarm %s", instant.isoformat())
        elif state.mode == "idle":
            actions += [StartRing(instant, kind), PublishState("ringing")]
            state = replace(state, mode="ringing", active=instant, active_kind=kind,
                            ring_started=now, snoozes=0)
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


def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)


def find_sink(pw_dump: object, needle: str) -> tuple[int, str] | None:
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
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        initial_volume: float = 0.3,
    ) -> None:
        self._sink = sink
        self._sound = sound
        self._runner = run
        self._popen = popen
        self._initial_volume = max(0.0, min(1.0, initial_volume))
        self._node: tuple[int, str] | None = None
        self._saved: tuple[float, bool] | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._volume: float | None = None
        self._spawned_at = 0.0

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        result = self._runner(args)
        if result.returncode != 0:
            raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[:200]}")
        return result

    def _resolve(self) -> tuple[int, str] | None:
        res = self._run(["pw-dump"])
        try:
            node = find_sink(json.loads(res.stdout or "[]"), self._sink)
        except ValueError:
            node = None
        if node is None:
            raise RuntimeError(f"no PipeWire Audio/Sink matching {self._sink!r}")
        return node

    def _target(self) -> str:
        return str(self._node[0]) if self._node else "@DEFAULT_AUDIO_SINK@"

    def _spawn(self) -> None:
        args = ["pw-play"]
        if self._node:
            args += ["--target", self._node[1]]
        args.append(str(self._sound))
        self._proc = self._popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._spawned_at = _time.monotonic()
        try:
            rc = self._proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            return
        raise RuntimeError(f"pw-play exited immediately ({rc})")

    def start(self) -> None:
        self.stop()
        self._node = self._resolve()
        target = self._target()
        # Refuse to change audio without a restorable snapshot.
        self._saved = parse_wpctl_volume(self._run(["wpctl", "get-volume", target]).stdout or "")
        try:
            self._run(["wpctl", "set-volume", target, f"{self._initial_volume:.2f}"])
            self._volume = self._initial_volume
            self._run(["wpctl", "set-mute", target, "0"])
            self._spawn()
        except Exception:
            self.stop()
            raise

    def tick(self, volume: float) -> None:
        if self._proc is None:
            return
        v = round(max(0.0, min(1.0, volume)), 2)
        if v != self._volume:
            self._run(["wpctl", "set-volume", self._target(), f"{v:.2f}"])
            self._volume = v
        rc = self._proc.poll()
        if rc is not None:
            if rc != 0 or _time.monotonic() - self._spawned_at < 0.25:
                raise RuntimeError(f"pw-play failed/ended prematurely ({rc})")
            self._spawn()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._saved is not None:
            vol, muted = self._saved
            target = self._target()
            try:
                self._run(["wpctl", "set-volume", target, f"{vol:.2f}"])
            finally:
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


def default_cache_path(config: Config) -> Path:
    if config.alarm.cache_path:
        return Path(config.alarm.cache_path)
    return Path(config.paths.db_path).parent / "alarm-cache.json"


class ActivityMonitor:
    """Dedicated hypridle listener. Its temporary config only targets our PID.

    The monitor is terminated and reaped before removing its config, so no
    listener remains that could signal a subsequently recycled PID.
    """

    def __init__(
        self, display: str = "", *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.display = display
        self._popen = popen
        self._proc: subprocess.Popen[bytes] | None = None
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._socket: Path | None = None
        self._retry_at = 0.0
        self._backoff = 5.0

    def tick(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None and self._socket is not None and self._socket.is_socket():
                return
            logger.warning("alarm daemon: activity monitor disconnected; retrying with backoff")
            self.stop()
            self._retry_at = _time.monotonic() + self._backoff
        if _time.monotonic() >= self._retry_at:
            self.start()

    def start(self) -> None:
        try:
            if shutil.which("hypridle") is None:
                raise RuntimeError("hypridle not on PATH")
            env = dict(os.environ)
            display = self.display or env.get("WAYLAND_DISPLAY", "")
            runtime = env.get("XDG_RUNTIME_DIR", "")
            candidate = Path(display) if Path(display).is_absolute() else Path(runtime) / display
            if not self.display and display and not candidate.is_socket():
                display = ""  # stale inherited display: rediscover after compositor restart
            if not display:
                runtime = env.get("XDG_RUNTIME_DIR", "")
                if runtime:
                    sockets = sorted(p for p in Path(runtime).glob("wayland-*") if p.is_socket())
                    if sockets:
                        display = sockets[0].name
            if not display:
                raise RuntimeError("no Wayland display/socket found")
            socket = Path(display)
            if not socket.is_absolute():
                socket = Path(env.get("XDG_RUNTIME_DIR", "/nonexistent")) / display
            if not socket.is_socket():
                raise RuntimeError(f"Wayland socket unavailable: {socket}")
            self._socket = socket
            env["WAYLAND_DISPLAY"] = display
            self._directory = tempfile.TemporaryDirectory(prefix="commute-alarm-activity-")
            config = Path(self._directory.name) / "hypridle.conf"
            config.write_text(
                "general {\n    ignore_dbus_inhibit = true\n    ignore_systemd_inhibit = true\n"
                "    ignore_wayland_inhibit = true\n}\n"
                "listener {\n    timeout = 1\n"
                f"    on-resume = kill -USR1 {os.getpid()}\n}}\n"
            )
            self._proc = self._popen(["hypridle", "-c", str(config)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if self._proc.poll() is not None:
                raise RuntimeError("hypridle exited during startup")
            self._backoff = 5.0
        except Exception as exc:  # noqa: BLE001 — activity is optional, audio isn't
            logger.warning("alarm daemon: activity monitor unavailable: %s", exc)
            self.stop()
            self._retry_at = _time.monotonic() + self._backoff
            self._backoff = min(60.0, self._backoff * 2)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._socket = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


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
    publish_audio_failed: Callable[[bool], bool] = lambda _f: True
    publish_handled: Callable[[datetime], bool] = lambda _t: True
    activity_monitor: ActivityMonitor | None = None
    clock: Callable[[], datetime] = now_nyc
    sleep: Callable[[float], None] = _time.sleep
    state: DaemonState = field(default_factory=DaemonState)
    _running: bool = True
    _last_heartbeat: datetime | None = None
    _audio_healthy: bool = True
    _audio_reported: bool | None = None
    _activity_pressed: datetime | None = None
    _resume_audio: bool = False
    _handled_pending: datetime | None = None
    _published_mode: Mode | None = None

    def __post_init__(self) -> None:
        self.state = self.cache.load()
        self._audio_healthy = not self.state.audio_failed
        self._resume_audio = self.state.mode == "ringing" and not self.state.audio_failed
        # Do not clear HA's failure latch merely because a process restarted.
        self._audio_reported = False if self._audio_healthy else None

    def run_once(self) -> float:
        """One poll; returns seconds to sleep before the next."""
        now = self.clock()
        inputs = self.reader()
        if self._activity_pressed is not None:
            inputs = replace(inputs, stop_pressed_at=self._activity_pressed)
            self._activity_pressed = None
        if self.activity_monitor is not None:
            self.activity_monitor.tick()
        before = self.state
        self.state, actions = step(self.state, now, inputs, self.settings)
        # Persist before playback so a restart can resume the same bounded
        # ring/snooze window rather than consume or extend it.
        if self._resume_audio:
            self._resume_audio = False
            if (
                self.state.mode == "ringing" and self.state.active is not None
                and not any(isinstance(action, StartRing) for action in actions)
            ):
                actions.insert(0, StartRing(self.state.active, self.state.active_kind))
        if before != self.state:
            self.cache.save(self.state)
        for action in actions:
            self._apply(action)
        if inputs.ha_ok:
            self._reconcile_mode()
        if self._audio_healthy and self.state.mode == "ringing" and self.state.ring_started is not None:
            try:
                elapsed = now.astimezone(UTC) - self.state.ring_started.astimezone(UTC)
                self.player.tick(ramp_fraction(elapsed, self.ramp_start_percent, self.ramp_seconds))
            except Exception as exc:  # noqa: BLE001 — let HA's dead-man fallback fire
                self._audio_healthy = False
                logger.error("alarm daemon: audio tick failed: %s", exc)
                self._stop_audio()
        if self.state.active != self._handled_pending or not self._audio_healthy:
            self._handled_pending = None
        if (
            inputs.ha_ok
            and self._handled_pending is not None
            and self.publish_handled(self._handled_pending)
        ):
            self._handled_pending = None
        self.state = replace(self.state, audio_failed=not self._audio_healthy)
        if self.state != before:
            self.cache.save(self.state)
        if (
            inputs.ha_ok
            and self._audio_reported != self.state.audio_failed
            and self.publish_audio_failed(self.state.audio_failed)
        ):
            self._audio_reported = self.state.audio_failed
        if inputs.ha_ok and self._audio_healthy and (
            self._last_heartbeat is None
            or (now.astimezone(UTC) - self._last_heartbeat.astimezone(UTC)).total_seconds() >= self.poll_seconds
        ) and self.heartbeat(now):
            self._last_heartbeat = now
        return RINGING_POLL_SECONDS if self.state.mode != "idle" else self.poll_seconds

    def _apply(self, action: Action) -> None:
        if isinstance(action, StartRing):
            logger.info("alarm daemon: ringing for %s alarm at %s", action.kind or "?", action.instant.isoformat())
            try:
                self.player.start()
                self._audio_healthy = True
                # A successful start may clear a failure left by an earlier
                # process even if this cache did not contain that failure.
                self._audio_reported = None
                self._handled_pending = action.instant
            except Exception as exc:  # noqa: BLE001 — keep polling; HA fallback alerts
                self._audio_healthy = False
                logger.error("alarm daemon: audio start failed: %s", exc)
                self._stop_audio()
        elif isinstance(action, StopRing):
            logger.info("alarm daemon: stopping (%s)", action.reason)
            self._stop_audio()
        # PublishState actions are coalesced to the final current mode below;
        # failed writes stay dirty and are retried on subsequent healthy polls.

    def _reconcile_mode(self) -> None:
        if self._published_mode == self.state.mode:
            return
        if self.publish_state(self.state.mode):
            self._published_mode = self.state.mode
        else:
            logger.warning("alarm daemon: failed to publish state %s", self.state.mode)

    def _stop_audio(self) -> None:
        try:
            self.player.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("alarm daemon: audio stop failed: %s", exc)

    def activity(self, *_: object) -> None:
        """SIGUSR1 is process-local and never persisted as a button timestamp."""
        if self.state.mode in ("ringing", "snoozed"):
            self._activity_pressed = self.clock()

    def request_stop(self, *_: object) -> None:
        self._running = False

    def run_forever(self) -> None:
        try:
            if self.activity_monitor is not None:
                self.activity_monitor.start()
            while self._running:
                delay = self.run_once()
                # Sleep in short slices so SIGTERM is honoured promptly.
                remaining = delay
                while self._running and remaining > 0:
                    chunk = min(1.0, remaining)
                    self.sleep(chunk)
                    remaining -= chunk
        finally:
            if self.activity_monitor is not None:
                self.activity_monitor.stop()
            self._stop_audio()
            # Process shutdown is not alarm completion. HA nap cleanup must
            # continue seeing the recoverable cached ringing/snoozed mode.
            self._reconcile_mode()


def build_daemon(config: Config) -> AlarmDaemon:
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
        # HA evaluates liveness using the unambiguous UTC text helper. Keep the
        # legacy local datetime for compatibility, but never let it mask a
        # failed authoritative heartbeat write.
        utc_ok = call_service(base_url, token, "input_text", "set_value",
                              {"entity_id": cfg.heartbeat_utc_entity, "value": _key(now)})
        call_service(base_url, token, "input_datetime", "set_datetime",
                     {"entity_id": cfg.heartbeat_entity, "timestamp": now.timestamp()})
        return utc_ok

    def publish_audio_failed(failed: bool) -> bool:
        return call_service(base_url, token, "input_boolean", "turn_on" if failed else "turn_off",
                            {"entity_id": cfg.audio_failed_entity})

    def publish_handled(instant: datetime) -> bool:
        return call_service(base_url, token, "input_text", "set_value",
                            {"entity_id": cfg.handled_entity, "value": _key(instant)})

    return AlarmDaemon(
        reader=HAReader(base_url, token, cfg).read,
        player=PipeWirePlayer(cfg.sink, sound, initial_volume=cfg.ramp_start_percent / 100),
        cache=AlarmCache(cache_path),
        settings=Settings.from_config(cfg),
        ramp_start_percent=cfg.ramp_start_percent,
        ramp_seconds=cfg.ramp_seconds,
        poll_seconds=float(cfg.poll_seconds),
        publish_state=publish_state,
        heartbeat=heartbeat,
        publish_audio_failed=publish_audio_failed,
        publish_handled=publish_handled,
        activity_monitor=ActivityMonitor(cfg.wayland_display) if cfg.stop_on_activity else None,
    )


def run_daemon(config: Config) -> None:
    """Entry point for ``alarm daemon``: install SIGTERM/SIGINT handlers and loop."""
    daemon = build_daemon(config)

    def _handler(signum: int, _frame: FrameType | None) -> None:
        logger.info("alarm daemon: signal %d — restoring audio and exiting", signum)
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGUSR1, daemon.activity)
    daemon.run_forever()
