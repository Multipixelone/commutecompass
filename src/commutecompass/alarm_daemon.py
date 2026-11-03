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
departure D satisfies ``wake == D - first_alarm_lead``)::

    window  ringing → snoozed   snooze or HA stop: min(now + 5 min, D - 13)
    window  ringing → snoozed   15 min unanswered: quiet until D - 13
    window  snoozed → ringing   snooze elapsed: re-ring 20%, hold 30 s, 60 s ramp
    window  *       → final     D - 13 reached (stale limit ignored until D)
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
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
    # Real departure D for the published commute wake (window mode only).
    departure_at: datetime | None = None
    departure_known: bool = False
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
    departure: datetime | None = None
    departure_known = False
    if cfg.window.enabled and cfg.window.departure_utc_entity:
        departure, departure_known = _helper_time(states, cfg.window.departure_utc_entity)
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
        departure_at=departure,
        departure_known=departure_known,
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
    # Commute wake window: D locked at first ring; phase '' = legacy alarm.
    departure: datetime | None = None
    phase: Phase = ""
    pending_departure: datetime | None = None
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
        departure = parse_ha_datetime(raw.get("departure"))
        raw_phase = raw.get("phase")
        phase: Phase = (
            raw_phase if raw_phase in ("window", "final") and departure is not None
            and restored_mode != "idle" else ""
        )
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
            departure=departure if phase else None,
            phase=phase,
            pending_departure=parse_ha_datetime(raw.get("pending_departure")),
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
            "departure": state.departure.isoformat() if state.departure else None,
            "phase": state.phase,
            "pending_departure": (
                state.pending_departure.isoformat() if state.pending_departure else None
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
    """Commute wake window, relative to the real departure time D."""

    first_lead: timedelta = timedelta(minutes=50)
    deadline_lead: timedelta = timedelta(minutes=13)
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
                   snooze_until=None, snoozes=0, departure=None, phase="")

def _minus(instant: datetime, delta: timedelta) -> datetime:
    """Absolute-time subtraction (DST-safe), returned in ``instant``'s zone."""
    return (instant.astimezone(UTC) - delta).astimezone(instant.tzinfo)

def _commute_departure(
    instant: datetime, kind: str, inputs: Inputs, state: DaemonState, settings: Settings,
) -> datetime | None:
    """D for a commute wake, or None (nap, fallback, window off, helpers disagree).

    Only an exact ``D - first_alarm_lead == wake`` match counts, so a stale or
    half-written departure helper degrades to the legacy alarm, never a window.
    """
    if settings.window is None or kind != "wake":
        return None
    sources = [state.pending_departure]
    if inputs.ha_ok and inputs.departure_known:
        sources.insert(0, inputs.departure_at)
    for departure in sources:
        if departure is not None and _key(_minus(departure, settings.window.first_lead)) == _key(instant):
            return departure
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
    departure = state.departure
    active = state.active
    assert departure is not None and active is not None
    # A replan may pull D earlier (same day) but never push the deadline later.
    if (
        inputs.ha_ok and inputs.departure_known and inputs.departure_at is not None
        and not _le(departure, inputs.departure_at)
        and to_nyc(inputs.departure_at).date() == to_nyc(departure).date()
    ):
        departure = inputs.departure_at
        state = replace(state, departure=departure)
    deadline = _minus(departure, window.deadline_lead)
    if state.snooze_until is not None and _le(deadline, state.snooze_until):
        state = replace(state, snooze_until=deadline)

    if inputs.activity_at is not None:
        # Desktop activity is the only dismissal, window or final.
        if state.mode == "ringing":
            actions.append(StopRing("activity"))
        actions += [PublishState("idle"), PublishWindowDone(now)]
        return _idle(state)
    if state.phase == "final":
        # No snooze, no HA stop, no timeout: ring until the desktop dismisses.
        return state
    if _le(deadline, now):
        if state.mode == "ringing":
            # Keep the current (already loud) ring; just stop honouring snooze.
            return replace(state, phase="final")
        if _le(departure, now):
            logger.warning("alarm daemon: commute window for %s missed (departure passed)",
                           departure.isoformat())
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
            # Go quiet, but the final alarm stays armed.
            actions += [StopRing("timeout"), PublishState("snoozed")]
            return replace(state, mode="snoozed", snooze_until=deadline,
                           snoozes=state.snoozes + 1)
        return state
    if state.mode == "snoozed" and state.snooze_until is not None and _le(state.snooze_until, now):
        state = replace(state, mode="ringing", ring_started=now, snooze_until=None)
        actions += [StartRing(active, state.active_kind, ring_profile(state, settings)),
                    PublishState("ringing")]
    return state


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
    if state.active is not None and (
        _window_cancelled(state, inputs) if state.phase
        else _cancelled(state.active, state.active_kind, inputs, preserve_rollover=True)
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
            # A due commute wake stays recoverable until its departure.
            or (state.pending_departure is not None and not _le(state.pending_departure, now))
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
    pending_departure = (
        _commute_departure(pending, kind, inputs, state, settings) if pending is not None else None
    )
    departures = {_key(t): _commute_departure(t, k, inputs, state, settings) for t, k in due}
    state = replace(state, pending=pending, pending_kind=kind, pending_departure=pending_departure)
    for instant, kind in due:
        state = _mark_done(state, instant)
        departure = departures[_key(instant)]
        if departure is not None:
            # Window alarms are bounded by D, not the stale limit, so a
            # restart can never skip the final alarm.
            if _le(departure, now):
                logger.warning("alarm daemon: skipping commute wake %s (departure passed)",
                               instant.isoformat())
            elif state.mode == "idle":
                deadline = _minus(departure, (settings.window or WindowSettings()).deadline_lead)
                actions += [StartRing(instant, kind), PublishState("ringing")]
                state = replace(state, mode="ringing", active=instant, active_kind=kind,
                                ring_started=now, snoozes=0, departure=departure,
                                phase="final" if _le(deadline, now) else "window")
            continue
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
    # In-flight volume commands must not hold stop/restoration for ten seconds.
    timeout = 0.5 if args[:2] == ["wpctl", "set-volume"] else 10
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


_AUDIO_TICK_SECONDS = 0.05  # 20 Hz; serial commands, never a catch-up burst


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
        try:
            initial = self._level(0.0)
            self._run(["wpctl", "set-volume", target, f"{initial:.2f}"])
            self._volume = initial
            self._run(["wpctl", "set-mute", target, "0"])
            self._spawn()
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
                    self._run(["wpctl", "set-volume", self._target(), f"{v:.4f}"])
                    self._volume = v
                if self._stop.is_set():
                    break
                if self._proc is not None:
                    rc = self._proc.poll()
                    if rc is not None:
                        if rc != 0 or now - self._spawned_at < 0.25:
                            raise RuntimeError(f"pw-play failed/ended prematurely ({rc})")
                        self._spawn()
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
    _window_done_pending: datetime | None = None

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
            inputs = replace(inputs, activity_at=self._activity_pressed)
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
                actions.insert(0, StartRing(self.state.active, self.state.active_kind,
                                            ring_profile(self.state, self.settings)))
        if before != self.state:
            self.cache.save(self.state)
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
            and self.publish_window_done(self._window_done_pending)
        ):
            self._window_done_pending = None
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

    if cfg.window.enabled and not cfg.stop_on_activity:
        logger.warning("alarm daemon: wake window final alarm can only be dismissed by "
                       "desktop activity, but stop_on_activity is off; it will ring until "
                       "the master switch is turned off or the wake is skipped")

    def publish_window_done(instant: datetime) -> bool:
        # Empty entity disables the hook (HA restores lights / clears notifications).
        if not cfg.window.window_done_entity:
            return True
        return call_service(base_url, token, "input_text", "set_value",
                            {"entity_id": cfg.window.window_done_entity, "value": _key(instant)})

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
