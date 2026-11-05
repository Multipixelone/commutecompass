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

Commute wake window (``[alarm.window]``; only wakes whose published
deadline D satisfies ``wake == D - first_alarm_lead``)::

    window  ringing → snoozed   snooze, HA stop, or 15 min timeout: min(now + 5 min, D)
    window  snoozed → ringing   snooze elapsed: re-ring 20%, hold 30 s, 60 s ramp
    window  *       → final     buffered door deadline D reached (first ring D - 37)
    restart *       → final     missed final recovered through D + 10 min inclusive
    final   ringing             snooze/HA stop ignored, no timeout
    any     *       → idle      desktop activity (publishes window_done_entity),
                                master switch off, explicit skip/clear
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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import FrameType
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

import httpx

from commutecompass.alarm_contract import ScheduleError, format_utc, parse_schedule
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
Phase = Literal["", "window", "final"]


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
    # Real deadline D for the published commute wake (window mode only).
    deadline_at: datetime | None = None
    deadline_known: bool = False
    # Local desktop activity (never from HA): the only final-alarm dismissal.
    activity_at: datetime | None = None


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
    v1 = cfg.schedule_contract == "v1"
    nxt = {} if v1 else states.get(cfg.next_alarm_entity, {})
    attrs = nxt.get("attributes") if isinstance(nxt.get("attributes"), dict) else {}
    kind = attrs.get("kind") if isinstance(attrs, dict) else ""
    enabled_state = states.get(cfg.enabled_entity, {}).get("state")
    # Publisher already subtracts wake_lead_minutes: this helper is HA's
    # effective hard cap, not the raw prep time. Never subtract the lead twice.
    threshold, prep_known = (None, False) if v1 else _helper_time(states, cfg.next_prep_utc_entity)
    wake, wake_known = (None, False) if v1 else _helper_time(states, cfg.wake_utc_entity)
    nap, nap_known = _helper_time(states, cfg.nap_utc_entity)
    skipped, _ = _helper_time(states, cfg.skipped_utc_entity)
    deadline: datetime | None = None
    deadline_known = False
    if not v1 and cfg.window.enabled and cfg.window.deadline_utc_entity:
        deadline, deadline_known = _helper_time(states, cfg.window.deadline_utc_entity)
    if cfg.schedule_contract == "v1":
        # A partial migration/write is no new wake knowledge, not a downgrade.
        wake = deadline = threshold = None
        wake_known = deadline_known = prep_known = False
        if states.get(cfg.contract_entity, {}).get("state") == "v1":
            try:
                schedule = parse_schedule(
                    states.get(cfg.schedule_entity, {}).get("state"),
                    first_lead_seconds=cfg.window.first_alarm_lead_minutes * 60,
                )
            except ScheduleError as exc:
                logger.warning("alarm daemon: ignoring invalid v1 schedule: %s", exc)
            else:
                wake, deadline = schedule.wake_at, schedule.deadline_at
                # Despite its name, next_prep_at is the publisher's effective first-alarm
                # / nap cap (WakeResult.next_prep_at), not raw prep. Never subtract twice.
                threshold = schedule.next_prep_at
                wake_known = deadline_known = prep_known = True
        # Do not even expose the legacy sensor as a v1 wake hint.
        nxt, kind = {}, ""
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
        deadline_at=deadline,
        deadline_known=deadline_known,
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
class SessionStatus:
    id: str
    original_at: datetime
    kind: str
    phase: str
    locked_deadline_at: datetime | None = None


@dataclass(frozen=True)
class RingStatus:
    id: str
    started_at: datetime
    playback: Literal["starting", "running", "stopped", "failed"] = "starting"
    checked_at: datetime | None = None


@dataclass(frozen=True)
class TerminalStatus:
    session_id: str
    original_at: datetime
    kind: str
    phase: str
    locked_deadline_at: datetime | None
    ended_at: datetime
    reason: str


@dataclass(frozen=True)
class DaemonState:
    mode: Mode = "idle"
    enabled: bool = True
    active_kind: str = ""
    audio_failed: bool = False
    window_done_pending: datetime | None = None
    prep_threshold: datetime | None = None
    # Latest known alarm instant (from HA, or the cache while HA is down).
    pending: datetime | None = None
    pending_kind: str = ""
    # Instant currently being serviced (ringing/snoozed).
    active: datetime | None = None
    ring_started: datetime | None = None
    snooze_until: datetime | None = None
    snoozes: int = 0
    # Commute wake window: L locked at first ring; phase '' = legacy alarm.
    deadline: datetime | None = None
    phase: Phase = ""
    pending_deadline: datetime | None = None
    # ISO-UTC instants already rung (or skipped as stale) — never re-ring.
    done: tuple[str, ...] = ()
    revision: int = 0
    session: SessionStatus | None = None
    ring: RingStatus | None = None
    ended_wake: TerminalStatus | None = None
    ended_nap: TerminalStatus | None = None

    def is_done(self, instant: datetime) -> bool:
        return _key(instant) in self.done


def _key(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _key(left) == _key(right)


def _mark_done(state: DaemonState, instant: datetime) -> DaemonState:
    if state.is_done(instant):
        return state
    return replace(state, done=(*state.done, _key(instant))[-_DONE_MAX:])


def _status_record(value: SessionStatus | RingStatus | TerminalStatus | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: format_utc(item) if isinstance(item, datetime) else item
            for key, item in asdict(value).items()}


def _load_session(raw: Any) -> SessionStatus | None:
    if not isinstance(raw, dict):
        return None
    instant = parse_ha_datetime(raw.get("original_at"))
    identity = raw.get("id")
    if (not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None
            or instant is None or raw.get("kind") not in ("wake", "nap")
            or raw.get("phase") not in ("legacy", "window", "final")):
        return None
    return SessionStatus(identity, instant, raw["kind"], raw["phase"],
                         parse_ha_datetime(raw.get("locked_deadline_at")))


def _load_ring(raw: Any, *, stopped: bool = False) -> RingStatus | None:
    if not isinstance(raw, dict):
        return None
    instant = parse_ha_datetime(raw.get("started_at"))
    identity = raw.get("id")
    if (not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None
            or instant is None):
        return None
    # A previous process's successful playback is not evidence of current health.
    return RingStatus(identity, instant, "stopped" if stopped else "starting")


def _load_terminal(raw: Any) -> TerminalStatus | None:
    if not isinstance(raw, dict):
        return None
    session = _load_session({**raw, "id": raw.get("session_id")})
    ended = parse_ha_datetime(raw.get("ended_at"))
    reason = raw.get("reason")
    if session is None or ended is None or not isinstance(reason, str):
        return None
    return _terminal(session, ended, reason)


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
        deadline = parse_ha_datetime(raw.get("deadline"))
        raw_phase = raw.get("phase")
        phase: Phase = (
            raw_phase if raw_phase in ("window", "final") and deadline is not None
            and restored_mode != "idle" else ""
        )
        active_kind = str(raw.get("active_kind") or "")
        locked_deadline = deadline if phase else None
        session = _load_session(raw.get("session")) if restored_mode != "idle" else None
        if session is not None and (
            not _same_instant(session.original_at, active) or session.kind != active_kind
            or session.phase != (phase or "legacy")
            or not _same_instant(session.locked_deadline_at, locked_deadline)
        ):
            session = None
        ring = _load_ring(raw.get("ring"), stopped=restored_mode == "snoozed") if session else None
        if ring is not None and not _same_instant(ring.started_at, ring_started):
            ring = None
        return DaemonState(
            mode=restored_mode,
            enabled=raw.get("enabled", True) is not False,
            active=active if restored_mode != "idle" else None,
            active_kind=active_kind,
            ring_started=ring_started,
            snooze_until=snooze_until,
            snoozes=max(0, snoozes) if isinstance(snoozes, int) else 0,
            audio_failed=raw.get("audio_failed") is True,
            window_done_pending=parse_ha_datetime(raw.get("window_done_pending")),
            prep_threshold=parse_ha_datetime(raw.get("prep_threshold")),
            pending=pending,
            pending_kind=str(raw.get("pending_kind") or ""),
            deadline=locked_deadline,
            phase=phase,
            revision=max(0, raw.get("revision", 0)) if type(raw.get("revision")) is int else 0,
            session=session,
            ring=ring,
            ended_wake=_load_terminal(raw.get("ended_wake")),
            ended_nap=_load_terminal(raw.get("ended_nap")),
            pending_deadline=parse_ha_datetime(raw.get("pending_deadline")),
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
            "window_done_pending": (state.window_done_pending.isoformat()
                                    if state.window_done_pending else None),
            "prep_threshold": state.prep_threshold.isoformat() if state.prep_threshold else None,
            "deadline": state.deadline.isoformat() if state.deadline else None,
            "phase": state.phase,
            "revision": state.revision,
            "session": _status_record(state.session),
            "ring": _status_record(state.ring),
            "ended_wake": _status_record(state.ended_wake),
            "ended_nap": _status_record(state.ended_nap),
            "pending_deadline": (
                state.pending_deadline.isoformat() if state.pending_deadline else None
            ),
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
class RingProfile:
    """Per-ring volume curve: hold ``start`` then ramp linearly to 100%."""

    start: float
    hold_seconds: float = 0.0
    ramp_seconds: float = 0.0


@dataclass(frozen=True)
class StartRing:
    instant: datetime
    kind: str
    # None = the player's configured first-alarm ramp.
    profile: RingProfile | None = None


@dataclass(frozen=True)
class StopRing:
    reason: str


@dataclass(frozen=True)
class PublishState:
    mode: Mode


@dataclass(frozen=True)
class PublishWindowDone:
    """A commute wake window was dismissed at the desktop."""

    at: datetime

Action = StartRing | StopRing | PublishState | PublishWindowDone


@dataclass(frozen=True)
class WindowSettings:
    """Commute wake window, relative to the real deadline time D."""

    first_lead: timedelta = timedelta(minutes=37)
    deadline_lead: timedelta = timedelta(0)
    snooze: timedelta = timedelta(minutes=5)
    rering: RingProfile = RingProfile(0.2, 30.0, 60.0)

@dataclass(frozen=True)
class Settings:
    """Subset of AlarmConfig the state machine needs."""

    max_ring: timedelta = timedelta(minutes=15)
    snooze: timedelta = timedelta(minutes=9)
    max_snoozes: int = 2
    stale_limit: timedelta = STALE_LIMIT
    # None disables the commute wake window (legacy behaviour for all alarms).
    window: WindowSettings | None = None

    @classmethod
    def from_config(cls, cfg: AlarmConfig) -> Settings:
        w = cfg.window
        return cls(
            max_ring=timedelta(minutes=cfg.max_ring_minutes),
            snooze=timedelta(minutes=cfg.snooze_minutes),
            max_snoozes=cfg.max_snoozes,
            window=WindowSettings(
                first_lead=timedelta(minutes=w.first_alarm_lead_minutes),
                deadline_lead=timedelta(minutes=w.deadline_lead_minutes),
                snooze=timedelta(minutes=w.snooze_minutes),
                rering=RingProfile(w.rering_start_percent / 100, float(w.rering_hold_seconds),
                                   float(w.rering_ramp_seconds)),
            ) if w.enabled else None,
        )


def _after(ts: datetime | None, ref: datetime | None) -> bool:
    return ts is not None and ref is not None and ts.astimezone(UTC) > ref.astimezone(UTC)


def _le(a: datetime, b: datetime) -> bool:
    return a.astimezone(UTC) <= b.astimezone(UTC)


def _cancelled(
    instant: datetime, kind: str, inputs: Inputs, *, preserve_rollover: bool = False,
    preserve_active_wake: bool = False,
) -> bool:
    """Backing helpers, not the future-only sensor, authoritatively cancel."""
    if not inputs.ha_ok:
        return False
    if inputs.skipped_at is not None and _key(instant) == _key(inputs.skipped_at):
        return True
    known = inputs.nap_known if kind == "nap" else inputs.wake_known
    helper = inputs.nap_at if kind == "nap" else inputs.wake_at
    # A publication owns pending scheduling, not an already active wake.
    # Explicit skip (above), clear and master-off still end active sessions.
    # Retain next-day rollover protection for cached elapsed pending wakes too.
    if (
        kind == "wake"
        and helper is not None
        and (preserve_active_wake or (
            preserve_rollover and to_nyc(helper).date() > to_nyc(instant).date()
        ))
    ):
        return False
    return known and (helper is None or _key(helper) != _key(instant))


def _idle(state: DaemonState) -> DaemonState:
    return replace(state, mode="idle", active=None, active_kind="", ring_started=None,
                   snooze_until=None, snoozes=0, deadline=None, phase="")

def _minus(instant: datetime, delta: timedelta) -> datetime:
    """Absolute-time subtraction (DST-safe), returned in ``instant``'s zone."""
    return (instant.astimezone(UTC) - delta).astimezone(instant.tzinfo)

def _commute_deadline(
    instant: datetime, kind: str, inputs: Inputs, state: DaemonState, settings: Settings,
) -> datetime | None:
    """D for a commute wake, or None (nap, fallback, window off, helpers disagree).

    Only an exact ``D - first_alarm_lead == wake`` match counts, so a stale or
    half-written deadline helper degrades to the legacy alarm, never a window.
    """
    if settings.window is None or kind != "wake":
        return None
    sources = [state.pending_deadline]
    if inputs.ha_ok and inputs.deadline_known:
        sources.insert(0, inputs.deadline_at)
    for deadline in sources:
        if deadline is not None and _key(_minus(deadline, settings.window.first_lead)) == _key(instant):
            return deadline
    return None

def _window_cancelled(state: DaemonState, inputs: Inputs) -> bool:
    """Only an explicit skip or clear ends a window; replans never skip the final."""
    if not inputs.ha_ok or state.active is None:
        return False
    if inputs.skipped_at is not None and _key(inputs.skipped_at) == _key(state.active):
        return True
    return inputs.wake_known and inputs.wake_at is None

def ring_profile(state: DaemonState, settings: Settings) -> RingProfile | None:
    """Volume curve for (re)starting the active ring: only window re-rings are quiet."""
    if state.phase == "window" and state.snoozes > 0:
        return (settings.window or WindowSettings()).rering
    return None

def _window_step(
    state: DaemonState, now: datetime, inputs: Inputs, settings: Settings, actions: list[Action],
) -> DaemonState:
    """Commute wake window: unlimited short snoozes, then an undismissable final."""
    window = settings.window or WindowSettings()
    deadline = state.deadline
    active = state.active
    assert deadline is not None and active is not None
    # A replan may pull D earlier (same day) but never push the deadline later.
    if (
        inputs.ha_ok and inputs.deadline_known and inputs.deadline_at is not None
        and not _le(deadline, inputs.deadline_at)
        and to_nyc(inputs.deadline_at).date() == to_nyc(deadline).date()
    ):
        deadline = inputs.deadline_at
        state = replace(state, deadline=deadline)
    deadline = _minus(deadline, window.deadline_lead)
    if state.snooze_until is not None and _le(deadline, state.snooze_until):
        state = replace(state, snooze_until=deadline)

    if inputs.activity_at is not None:
        # Desktop activity is the only dismissal, window or final.
        if state.mode == "ringing":
            actions.append(StopRing("activity"))
        actions += [PublishState("idle"), PublishWindowDone(now)]
        return replace(_idle(state), window_done_pending=now)
    if state.phase == "final":
        # No snooze, no HA stop, no timeout: ring until the desktop dismisses.
        return state
    if _le(deadline, now):
        if state.mode == "ringing":
            # Keep the current sound; just stop honouring snooze.
            return replace(state, phase="final")
        if now.astimezone(UTC) - deadline.astimezone(UTC) > settings.stale_limit:
            logger.warning("alarm daemon: commute window for %s missed (final recovery expired)",
                           deadline.isoformat())
            actions.append(PublishState("idle"))
            return _idle(state)
        actions += [StartRing(active, state.active_kind), PublishState("ringing")]
        return replace(state, mode="ringing", phase="final", ring_started=now, snooze_until=None)
    if state.mode == "ringing":
        # HA stop is never a dismissal here: treat it like snooze.
        if _after(inputs.snooze_pressed_at, state.ring_started) or _after(
            inputs.stop_pressed_at, state.ring_started,
        ):
            until = (now.astimezone(UTC) + window.snooze).astimezone(now.tzinfo)
            if _le(deadline, until):
                until = deadline
            actions += [StopRing("snooze"), PublishState("snoozed")]
            return replace(state, mode="snoozed", snooze_until=until, snoozes=state.snoozes + 1)
        if state.ring_started is not None and (
            now.astimezone(UTC) - state.ring_started.astimezone(UTC) >= settings.max_ring
        ):
            # Auto-snooze briefly, then re-ring even without a button press.
            until = min(_minus(now, -window.snooze), deadline, key=lambda d: d.astimezone(UTC))
            actions += [StopRing("timeout"), PublishState("snoozed")]
            return replace(state, mode="snoozed", snooze_until=until,
                           snoozes=state.snoozes + 1)
        return state
    if state.mode == "snoozed" and state.snooze_until is not None and _le(state.snooze_until, now):
        state = replace(state, mode="ringing", ring_started=now, snooze_until=None)
        actions += [StartRing(active, state.active_kind, ring_profile(state, settings)),
                    PublishState("ringing")]
    return state


def _step(
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

    # Explicit skips/clears cancel active wakes; replacement publications do not.
    # Nap rescheduling retains its existing cancellation behavior.
    if state.active is not None and (
        _window_cancelled(state, inputs) if state.phase
        else _cancelled(state.active, state.active_kind, inputs, preserve_active_wake=True)
    ):
        if state.mode == "ringing":
            actions.append(StopRing("cancelled"))
        if state.mode != "idle":
            actions.append(PublishState("idle"))
        state = _idle(state)
    if state.pending is not None and _cancelled(
        state.pending, state.pending_kind, inputs,
        preserve_rollover=_le(state.pending, now) and (
            now.astimezone(UTC) - state.pending.astimezone(UTC) <= settings.stale_limit
            # Let the anchored recovery branch ring or log expiry, even when
            # HA has rolled over to tomorrow. Explicit clear/skip still cancels.
            or state.pending_deadline is not None
        ),
    ):
        state = _mark_done(state, state.pending)
        state = replace(state, pending=None, pending_kind="")

    stop = _after(inputs.stop_pressed_at, state.ring_started) or _after(
        inputs.activity_at, state.ring_started,
    )
    if state.phase and state.mode != "idle":
        state = _window_step(state, now, inputs, settings, actions)
    elif state.mode == "ringing":
        if stop:
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
        if stop:
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
    pending_deadline = (
        _commute_deadline(pending, kind, inputs, state, settings) if pending is not None else None
    )
    deadlines = {_key(t): _commute_deadline(t, k, inputs, state, settings) for t, k in due}
    state = replace(state, pending=pending, pending_kind=kind, pending_deadline=pending_deadline)
    for instant, kind in due:
        state = _mark_done(state, instant)
        anchor = deadlines[_key(instant)]
        if anchor is not None:
            final_at = _minus(anchor, (settings.window or WindowSettings()).deadline_lead)
            if now.astimezone(UTC) - final_at.astimezone(UTC) > settings.stale_limit:
                logger.warning("alarm daemon: skipping commute wake %s (final recovery expired)",
                               instant.isoformat())
            elif state.mode == "idle":
                actions += [StartRing(instant, kind), PublishState("ringing")]
                state = replace(state, mode="ringing", active=instant, active_kind=kind,
                                ring_started=now, snoozes=0, deadline=anchor,
                                phase="final" if _le(final_at, now) else "window")
            continue
        if now.astimezone(UTC) - instant.astimezone(UTC) > settings.stale_limit:
            logger.warning("alarm daemon: skipping stale alarm %s", instant.isoformat())
        elif state.mode == "idle":
            actions += [StartRing(instant, kind), PublishState("ringing")]
            state = replace(state, mode="ringing", active=instant, active_kind=kind,
                            ring_started=now, snoozes=0)
    return state, actions


def _session(state: DaemonState, instant: datetime, kind: str) -> SessionStatus:
    return SessionStatus(uuid4().hex, instant, kind, state.phase or "legacy", state.deadline)


def _terminal(session: SessionStatus, now: datetime, reason: str) -> TerminalStatus:
    return TerminalStatus(session.id, session.original_at, session.kind, session.phase,
                          session.locked_deadline_at, now, reason)


def _status_semantics(state: DaemonState) -> tuple[object, ...]:
    """Revision tracks exported meaning, not observation/check times or delivery attempts."""
    ring = replace(state.ring, checked_at=None) if state.ring is not None else None
    return (state.mode, state.session, ring, state.audio_failed, state.snooze_until,
            state.ended_wake, state.ended_nap)


def _advance_revision(previous: DaemonState, current: DaemonState) -> DaemonState:
    revision = previous.revision + (_status_semantics(previous) != _status_semantics(current))
    return replace(current, revision=revision)


def _cancellation_reason(instant: datetime, kind: str, inputs: Inputs) -> str:
    if _same_instant(inputs.skipped_at, instant):
        return "skip"
    replacement = inputs.nap_at if kind == "nap" else inputs.wake_at
    return "schedule_cleared" if replacement is None else "schedule_replaced"


def _record_terminal(state: DaemonState, terminal: TerminalStatus) -> DaemonState:
    if terminal.kind == "nap":
        return replace(state, ended_nap=terminal)
    return replace(state, ended_wake=terminal)


def step(
    state: DaemonState, now: datetime, inputs: Inputs, settings: Settings
) -> tuple[DaemonState, list[Action]]:
    """Advance scheduling and persistable logical identities, independent of HA export."""
    if state.active is not None and state.session is None:
        state = replace(state, session=_session(state, state.active, state.active_kind))
    if state.mode == "ringing" and state.ring is None and state.ring_started is not None:
        state = replace(state, ring=RingStatus(uuid4().hex, state.ring_started))
    previous = state
    state, actions = _step(state, now, inputs, settings)
    reason = ""
    if previous.active is not None and not _same_instant(state.active, previous.active):
        if not state.enabled:
            reason = "master_off"
        elif (_window_cancelled(previous, inputs) if previous.phase else _cancelled(
            previous.active, previous.active_kind, inputs, preserve_active_wake=True,
        )):
            reason = _cancellation_reason(previous.active, previous.active_kind, inputs)
        elif (any(isinstance(a, PublishWindowDone) for a in actions)
              or _after(inputs.activity_at, previous.ring_started)):
            reason = "desktop_activity"
        elif (previous.deadline is not None and now.astimezone(UTC)
              - _minus(previous.deadline, (settings.window or WindowSettings()).deadline_lead).astimezone(UTC)
              > settings.stale_limit):
            reason = "recovery_expired"
        elif _after(inputs.stop_pressed_at, previous.ring_started):
            reason = "legacy_stop"
        else:
            reason = "legacy_timeout"
        if previous.session is not None:
            terminal = _terminal(previous.session, now, reason)
            state = _record_terminal(state, terminal)
    # Cancellation or missed recovery may consume a cached candidate before its first ring.
    if state.active is None and not reason:
        candidates = [(previous.pending, previous.pending_kind, previous.pending_deadline),
                      (inputs.wake_at, "wake", inputs.deadline_at), (inputs.nap_at, "nap", None)]
        for instant, kind, deadline in candidates:
            if instant is None or previous.is_done(instant):
                continue
            disabled_pending = not state.enabled and _same_instant(instant, previous.pending)
            if not state.is_done(instant) and not disabled_pending:
                continue
            if not state.enabled:
                reason = "master_off"
            elif inputs.ha_ok and _cancelled(instant, kind, inputs):
                reason = _cancellation_reason(instant, kind, inputs)
            else:
                reason = "recovery_expired"
            session = SessionStatus(uuid4().hex, instant, kind, "window" if deadline else "legacy", deadline)
            terminal = _terminal(session, now, reason)
            state = _record_terminal(state, terminal)
            break
    if state.active is None:
        state = replace(state, session=None, ring=None)
    else:
        existing_session = previous.session if _same_instant(state.active, previous.active) else None
        session = existing_session or _session(state, state.active, state.active_kind)
        session = replace(session, phase=state.phase or "legacy", locked_deadline_at=state.deadline)
        ring = previous.ring if _same_instant(state.active, previous.active) else None
        if any(isinstance(a, StartRing) for a in actions):
            ring = RingStatus(uuid4().hex, state.ring_started or now)
        elif state.mode != "ringing" and ring is not None:
            ring = replace(ring, playback="stopped", checked_at=now)
        state = replace(state, session=session, ring=ring)
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
    def start(self, profile: RingProfile | None = None) -> None:
        """Begin (looping) playback; must save prior volume/mute first.

        ``profile=None`` keeps the player's configured first-alarm ramp.
        """

    def tick(self) -> None:
        """Report local playback/worker failures without driving audio timing."""

    def stop(self) -> None:
        """Stop playback and restore saved volume/mute.  Idempotent."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # Allow PipeWire's slow first call without an unbounded stop/restore delay.
    timeout = 3 if args[:2] == ["wpctl", "set-volume"] else 10
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


_AUDIO_TICK_SECONDS = 0.05  # 20 Hz; serial commands, never a catch-up burst
_MAX_VOLUME_FAILURES = 3
_PLAYBACK_RETRY_DELAYS = (0.25, 0.5, 1.0)


def _audio_wait(stop: Event, delay: float) -> bool:
    return stop.wait(delay)


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
        ramp_seconds: int = 120,
        monotonic: Callable[[], float] = _time.monotonic,
        wait: Callable[[Event, float], bool] = _audio_wait,
    ) -> None:
        self._sink = sink
        self._sound = sound
        self._runner = run
        self._popen = popen
        self._initial_volume = max(0.0, min(1.0, initial_volume))
        self._ramp_seconds = ramp_seconds
        self._monotonic = monotonic
        self._wait = wait
        self._stop = Event()
        self._worker: Thread | None = None
        self._error: Exception | None = None
        self._ramp_origin = 0.0
        self._curve = RingProfile(self._initial_volume, 0.0, float(ramp_seconds))
        self._node: tuple[int, str] | None = None
        self._saved: tuple[float, bool] | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._volume: float | None = None
        self._spawned_at = 0.0
        self._volume_failures = 0
        self._volume_degraded = False
        self._playback_retries = 0

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
        self._spawned_at = self._monotonic()
        try:
            rc = self._proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            return
        raise RuntimeError(f"pw-play exited immediately ({rc})")

    def _spawn_with_retries(self) -> bool:
        """Share a finite abnormal-start budget across the entire ring."""
        while not self._stop.is_set():
            try:
                self._spawn()
                return True
            except Exception as exc:  # noqa: BLE001 — bounded recovery at playback boundary
                self._playback_retry(exc)
        return False

    def _playback_retry(self, error: Exception) -> None:
        if self._stop.is_set():
            return
        if self._playback_retries >= len(_PLAYBACK_RETRY_DELAYS):
            raise RuntimeError("pw-play recovery exhausted") from error
        delay = _PLAYBACK_RETRY_DELAYS[self._playback_retries]
        self._playback_retries += 1
        logger.warning("alarm playback retry %d: %s", self._playback_retries, error)
        self._wait(self._stop, delay)

    def _set_volume(self, volume: float) -> None:
        """Volume control is best effort; it must never terminate playback."""
        if self._volume_degraded or self._stop.is_set():
            return
        try:
            self._run(["wpctl", "set-volume", self._target(), f"{volume:.4f}"])
        except Exception as exc:  # noqa: BLE001 — gain failure must not stop playback
            self._volume_failures += 1
            logger.warning("alarm volume step failed (%d): %s", self._volume_failures, exc)
            if self._volume_failures >= _MAX_VOLUME_FAILURES:
                self._volume_degraded = True
                logger.warning("alarm volume control degraded; attempting full volume once")
                if not self._stop.is_set():
                    try:
                        self._run(["wpctl", "set-volume", self._target(), "1.0000"])
                        self._volume = 1.0
                    except Exception as fallback_exc:  # noqa: BLE001 — best-effort gain only
                        logger.warning("alarm full-volume fallback failed: %s", fallback_exc)
            return
        self._volume = volume
        self._volume_failures = 0

    def _level(self, elapsed: float) -> float:
        curve = self._curve
        if curve.ramp_seconds <= 0:
            return 1.0 if elapsed >= curve.hold_seconds else curve.start
        fraction = min(1.0, max(0.0, elapsed - curve.hold_seconds) / curve.ramp_seconds)
        return round(curve.start + (1.0 - curve.start) * fraction, 4)

    def start(self, profile: RingProfile | None = None) -> None:
        self.stop()
        if profile is None:
            self._curve = RingProfile(self._initial_volume, 0.0, float(self._ramp_seconds))
        else:
            self._curve = RingProfile(
                max(0.0, min(1.0, profile.start)),
                max(0.0, profile.hold_seconds),
                max(0.0, profile.ramp_seconds),
            )
        self._error = None
        self._stop.clear()
        self._node = self._resolve()
        target = self._target()
        # Refuse to change audio without a restorable snapshot.
        self._saved = parse_wpctl_volume(self._run(["wpctl", "get-volume", target]).stdout or "")
        if self._saved is None:
            raise RuntimeError("cannot snapshot sink volume")
        self._volume = None
        self._volume_failures = 0
        self._volume_degraded = False
        self._playback_retries = 0
        try:
            self._set_volume(self._level(0.0))
            self._run(["wpctl", "set-mute", target, "0"])
            if not self._spawn_with_retries():
                return
            # First launch, not setup/network time or later file-loop launches.
            self._ramp_origin = self._spawned_at
            worker = Thread(target=self._audio_loop, name="alarm-audio", daemon=True)
            worker.start()
            self._worker = worker
        except Exception:
            self.stop()
            raise

    def tick(self) -> None:
        if self._error is not None:
            raise RuntimeError("alarm audio worker failed") from self._error

    def _audio_loop(self) -> None:
        # Sole audio owner until stop joins us. Serial commands, no queued volumes.
        try:
            while not self._stop.is_set():
                now = self._monotonic()
                elapsed = max(0.0, now - self._ramp_origin)
                v = self._level(elapsed)
                if self._volume != v:
                    self._set_volume(v)
                if self._stop.is_set():
                    break
                if self._proc is not None:
                    rc = self._proc.poll()
                    if rc is not None:
                        if rc != 0 or self._monotonic() - self._spawned_at < 0.25:
                            self._playback_retry(
                                RuntimeError(f"pw-play failed/ended prematurely ({rc})"),
                            )
                        if not self._spawn_with_retries():
                            break
                # Slow commands skip slots instead of causing catch-up bursts.
                # Actual monotonic elapsed time, not a tick count, determines gain.
                if self._wait(self._stop, _AUDIO_TICK_SECONDS):
                    break
        except Exception as exc:
            logger.exception("alarm local audio failed")
            self._error = exc
            if not self._stop.is_set():
                try:
                    # Restore even if HA is blocked, unless stop owns cleanup.
                    self._restore()
                except Exception:
                    logger.exception("alarm audio restoration failed")

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join()
            self._worker = None
        # No stale volume writes after restoration or during the next ring.
        self._restore()

    def _restore(self) -> None:
        errors: list[Exception] = []
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception as exc:  # noqa: BLE001 — finish cleanup before reporting
                errors.append(exc)
            try:
                proc.wait(timeout=3)
            except Exception as exc:  # noqa: BLE001 — kill/reap even after a wait error
                if not isinstance(exc, subprocess.TimeoutExpired):
                    errors.append(exc)
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception as kill_exc:  # noqa: BLE001 — still restore the sink
                    # Keep the handle: a later stop must retry, not orphan it.
                    errors.append(kill_exc)
                else:
                    self._proc = None
            else:
                self._proc = None
        if self._saved is not None:
            vol, muted = self._saved
            target = self._target()
            restored = True
            # Attempt both even when process cleanup or one sink command fails.
            for args in (
                ["wpctl", "set-volume", target, f"{vol:.2f}"],
                ["wpctl", "set-mute", target, "1" if muted else "0"],
            ):
                try:
                    self._run(args)
                except Exception as exc:  # noqa: BLE001 — attempt both, retain failed snapshot
                    restored = False
                    errors.append(exc)
            if restored:
                self._saved = None
        if errors:
            raise errors[0]


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
    poll_seconds: float = 30.0
    # Side-effect callbacks to HA (best-effort, return success).
    publish_state: Callable[[Mode], bool] = lambda _m: True
    heartbeat: Callable[[datetime], bool] = lambda _t: True
    publish_audio_failed: Callable[[bool], bool] = lambda _f: True
    publish_handled: Callable[[datetime], bool] = lambda _t: True
    publish_window_done: Callable[[datetime], bool] = lambda _t: True
    publish_status: Callable[[dict[str, Any]], bool] | None = None
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
    _restoring_window: bool = False
    _handled_pending: datetime | None = None
    _published_mode: Mode | None = None
    _window_done_pending: datetime | None = None

    def __post_init__(self) -> None:
        self.state = self.cache.load()
        self._audio_healthy = not self.state.audio_failed
        self._window_done_pending = self.state.window_done_pending
        self._resume_audio = self.state.mode == "ringing" and not self.state.audio_failed
        self._restoring_window = bool(self.state.phase and self.state.mode == "ringing")
        # Do not clear HA's failure latch merely because a process restarted.
        self._audio_reported = False if self._audio_healthy else None

    def run_once(self) -> float:
        """One poll; returns seconds to sleep before the next."""
        now = self.clock()
        try:
            inputs = self.reader()
        except Exception as exc:  # noqa: BLE001 — cached alarms survive reader failures
            logger.warning("alarm daemon: reader failed: %s", type(exc).__name__)
            inputs = Inputs(ha_ok=False)
        if self._activity_pressed is not None:
            inputs = replace(inputs, activity_at=self._activity_pressed)
            self._activity_pressed = None
        if self.activity_monitor is not None:
            self.activity_monitor.tick()
        before = self.state
        if self._restoring_window:
            # A cached ring is not currently audible. Apply missed-final bounds,
            # unlike a live ring which must continue indefinitely in final phase.
            deadline = self.state.deadline
            if (inputs.ha_ok and inputs.deadline_known
                    and inputs.deadline_at is not None and deadline is not None
                    and to_nyc(inputs.deadline_at).date() == to_nyc(deadline).date()):
                deadline = min(deadline, inputs.deadline_at, key=lambda d: d.astimezone(UTC))
            if deadline is not None:
                final_at = _minus(deadline, (self.settings.window or WindowSettings()).deadline_lead)
                expired = now.astimezone(UTC) - final_at.astimezone(UTC) > self.settings.stale_limit
                if _le(final_at, now) and (self._resume_audio or expired):
                    self.state = replace(self.state, mode="snoozed", phase="window", snooze_until=now)
            self._restoring_window = False
        self.state, actions = step(self.state, now, inputs, self.settings)
        if (self._resume_audio and _same_instant(before.active, self.state.active)
                and before.mode == self.state.mode == "ringing" and before.ring is not None):
            self.state = replace(self.state, ring=before.ring, ring_started=before.ring_started)
        # Persist before playback so a restart can resume the same bounded
        # ring/snooze window rather than consume or extend it.
        if self._resume_audio:
            self._resume_audio = False
            if (
                self.state.mode == "ringing" and self.state.active is not None
                and not any(isinstance(action, StartRing) for action in actions)
            ):
                actions.insert(0, StartRing(self.state.active, self.state.active_kind,
                                            ring_profile(self.state, self.settings)))
        self.state = _advance_revision(before, self.state)
        if self.state != before:
            self.cache.save(self.state)
        before_audio = self.state
        for action in actions:
            self._apply(action)
        if inputs.ha_ok:
            self._reconcile_mode()
        if self._audio_healthy and self.state.mode == "ringing" and self.state.ring_started is not None:
            try:
                self.player.tick()
            except Exception as exc:  # noqa: BLE001 — let HA's dead-man fallback fire
                self._audio_healthy = False
                logger.error("alarm daemon: audio tick failed: %s", exc)
                self._stop_audio()
        if (
            inputs.ha_ok
            and self._window_done_pending is not None
            and self._safe_callback(
                "publish_window_done", self.publish_window_done, self._window_done_pending
            )
        ):
            self._window_done_pending = None
            self.state = replace(self.state, window_done_pending=None)
        if self.state.active != self._handled_pending or not self._audio_healthy:
            self._handled_pending = None
        if (
            inputs.ha_ok
            and self._handled_pending is not None
            and self._safe_callback("publish_handled", self.publish_handled, self._handled_pending)
        ):
            self._handled_pending = None
        if self.state.ring is not None and self.state.mode == "ringing":
            playback: Literal["running", "failed"] = "running" if self._audio_healthy else "failed"
            self.state = replace(self.state, ring=replace(self.state.ring, playback=playback,
                                                          checked_at=self.clock()))
        self.state = replace(self.state, audio_failed=not self._audio_healthy)
        self.state = _advance_revision(before_audio, self.state)
        if self.state != before_audio:
            self.cache.save(self.state)
        if (
            inputs.ha_ok
            and self._audio_reported != self.state.audio_failed
            and self._safe_callback(
                "publish_audio_failed", self.publish_audio_failed, self.state.audio_failed
            )
        ):
            self._audio_reported = self.state.audio_failed
        if inputs.ha_ok and self._audio_healthy and (
            self._last_heartbeat is None
            or (now.astimezone(UTC) - self._last_heartbeat.astimezone(UTC)).total_seconds() >= self.poll_seconds
        ) and self._safe_callback("heartbeat", self.heartbeat, now):
            self._last_heartbeat = now
        if inputs.ha_ok and self.publish_status is not None:
            try:
                self.publish_status(status_snapshot(self.state, self.clock()))
            except Exception as exc:  # noqa: BLE001 — advisory export must never gate controls
                logger.warning("alarm daemon: publish_status failed: %s", type(exc).__name__)
        return RINGING_POLL_SECONDS if self.state.mode != "idle" else self.poll_seconds

    def _apply(self, action: Action) -> None:
        if isinstance(action, StartRing):
            logger.info("alarm daemon: ringing for %s alarm at %s", action.kind or "?", action.instant.isoformat())
            try:
                if action.profile is None:
                    self.player.start()
                else:
                    self.player.start(action.profile)
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
        elif isinstance(action, PublishWindowDone):
            logger.info("alarm daemon: commute wake dismissed at desktop")
            self._window_done_pending = action.at
        # PublishState actions are coalesced to the final current mode below;
        # failed writes stay dirty and are retried on subsequent healthy polls.

    def _safe_callback[T](
        self, operation: str, callback: Callable[[T], bool], value: T,
    ) -> bool:
        """Optional publishers fail independently without acknowledging their payload."""
        try:
            return callback(value)
        except Exception as exc:  # noqa: BLE001 — local ringing must survive optional HA callbacks
            logger.warning("alarm daemon: %s failed: %s", operation, type(exc).__name__)
            return False

    def _reconcile_mode(self) -> None:
        if self._published_mode == self.state.mode:
            return
        if self._safe_callback("publish_state", self.publish_state, self.state.mode):
            self._published_mode = self.state.mode

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
                try:
                    self.activity_monitor.stop()
                except Exception as exc:  # noqa: BLE001 — cleanup steps are independent
                    logger.warning("alarm daemon: activity cleanup failed: %s", type(exc).__name__)
            self._stop_audio()  # Already guards player failures.
            # Process shutdown is not alarm completion. HA nap cleanup must
            # continue seeing the recoverable cached ringing/snoozed mode.
            try:
                self._reconcile_mode()
            except Exception as exc:  # noqa: BLE001 — do not mask shutdown's original error
                logger.warning("alarm daemon: final mode publication failed: %s", type(exc).__name__)


def status_snapshot(state: DaemonState, now: datetime) -> dict[str, Any]:
    """Small HA REST state payload. Never used as scheduling authority."""
    return {"state": state.mode, "attributes": {
        "schema": 1, "revision": state.revision, "observed_at": format_utc(now),
        "session": _status_record(state.session), "ring": _status_record(state.ring),
        "audio_failed": state.audio_failed,
        "snooze_until": format_utc(state.snooze_until) if state.snooze_until else None,
        "ended": {"wake": _status_record(state.ended_wake), "nap": _status_record(state.ended_nap)},
    }}


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

    if cfg.window.enabled and (not cfg.stop_on_activity or shutil.which("hypridle") is None):
        logger.warning("alarm daemon: wake window has no desktop activity monitor; "
                       "final will ring until master-off or explicit skip/clear")

    def publish_window_done(instant: datetime) -> bool:
        # Empty entity disables the hook (HA restores lights / clears notifications).
        if not cfg.window.window_done_entity:
            return True
        return call_service(base_url, token, "input_text", "set_value",
                            {"entity_id": cfg.window.window_done_entity, "value": _key(instant)})

    def publish_status(payload: dict[str, Any]) -> bool:
        # One attempt with phase/inactivity timeouts, not a total wall-clock deadline.
        # Inspect headers only: an arbitrary/trickling response body must not delay controls.
        try:
            timeout = httpx.Timeout(connect=0.5, read=0.5, write=0.5, pool=0.5)
            with httpx.Client(timeout=timeout) as client, client.stream(
                "POST", f"{base_url.rstrip('/')}/api/states/{cfg.status_entity}",
                headers={"Authorization": f"Bearer {token}"}, json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    logger.warning("alarm daemon: status export returned %s", response.status_code)
                    return False
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("alarm daemon: status export failed: %s", exc)
            return False
        return True

    return AlarmDaemon(
        reader=HAReader(base_url, token, cfg).read,
        player=PipeWirePlayer(cfg.sink, sound, initial_volume=cfg.ramp_start_percent / 100,
                              ramp_seconds=cfg.ramp_seconds),
        cache=AlarmCache(cache_path),
        settings=Settings.from_config(cfg),
        poll_seconds=float(cfg.poll_seconds),
        publish_state=publish_state,
        heartbeat=heartbeat,
        publish_audio_failed=publish_audio_failed,
        publish_handled=publish_handled,
        publish_window_done=publish_window_done,
        publish_status=publish_status if cfg.status_entity else None,
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
