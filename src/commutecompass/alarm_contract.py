"""Alarm HA contract v1 — schedule snapshot wire format + status freshness.

See ``docs/alarm-ha-contract.md``.  Pure functions, no I/O, no dependencies
beyond the standard library, so the publisher (``alarm.py``), the daemon and
tests share one definition of the wire format.

Schedule snapshot (stored verbatim in ``input_text.commute_alarm_schedule_v1``)::

    {"v":1,"id":"<32 lowercase hex>","issued_at":"<utc>","kind":"commute",
     "wake_at":"<utc>|null","deadline_at":"<utc>|null","next_prep_at":"<utc>|null"}

* Keys appear exactly in that order, compact separators, ASCII JSON.
* ``<utc>`` is ``YYYY-MM-DDTHH:MM:SSZ``, or ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
  when (and only when) microseconds are nonzero.
* Kinds: ``commute`` (wake + deadline required, ``deadline - wake ==
  first_lead_seconds``), ``legacy`` (wake required, deadline null), ``none``
  (all three null).  ``next_prep_at`` is optional except for ``none``.
* :func:`parse_schedule` accepts only the exact canonical string; anything
  else is :class:`ScheduleError` ("no new information", never cancellation).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

SCHEDULE_VERSION = 1
MAX_SNAPSHOT_LENGTH = 255
MANUAL_SNOOZE_SECONDS = 390
MAX_MANUAL_SNOOZES = 12
DEFAULT_FIRST_LEAD_SECONDS = 37 * 60  # 2220: approved window first-alarm lead
STATUS_MIN_FRESHNESS_SECONDS = 90

ScheduleKind = Literal["commute", "legacy", "none", "manual"]
ScheduleOrder = Literal["same", "newer", "stale", "conflict"]

_KEYS = ("v", "id", "issued_at", "kind", "wake_at", "deadline_at", "next_prep_at")
_KINDS: tuple[ScheduleKind, ...] = ("commute", "legacy", "none", "manual")
_ID_RE = re.compile(r"[0-9a-f]{32}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?Z")


class ScheduleError(ValueError):
    """Snapshot is malformed, non-canonical or violates the kind rules."""


def format_utc(value: datetime) -> str:
    """Canonical UTC ``Z`` timestamp; six microsecond digits only when nonzero.

    Raises :class:`ScheduleError` for naive datetimes.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError("timestamp must be timezone-aware")
    u = value.astimezone(UTC)
    # isoformat zero-pads the year (strftime %Y does not on every platform).
    spec = "microseconds" if u.microsecond else "seconds"
    return u.isoformat(timespec=spec).replace("+00:00", "Z")


def parse_utc(raw: object) -> datetime:
    """Parse a canonical UTC timestamp string into an aware UTC datetime.

    Rejects non-strings, offsets other than ``Z``, ``.000000`` fractions and
    impossible dates.  Raises :class:`ScheduleError`.
    """
    if not isinstance(raw, str) or not _UTC_RE.fullmatch(raw):
        raise ScheduleError(f"not a canonical UTC timestamp: {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw[:-1]).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ScheduleError(f"invalid UTC timestamp: {raw!r}") from exc
    if format_utc(parsed) != raw:
        raise ScheduleError(f"non-canonical UTC timestamp: {raw!r}")
    return parsed


@dataclass(frozen=True)
class ScheduleSnapshot:
    """Validated v1 schedule.  All datetimes are aware UTC.

    Construct via :func:`new_schedule` / :func:`parse_schedule`; direct
    construction is not validated until :func:`serialize_schedule`.
    """

    id: str
    issued_at: datetime
    kind: ScheduleKind
    wake_at: datetime | None
    deadline_at: datetime | None
    next_prep_at: datetime | None
    manual_snoozes: int | None = None


def validate_manual_snoozes(value: object) -> int:
    """Reject booleans, floats and out-of-range manual counts."""
    if type(value) is not int or not 0 <= value <= MAX_MANUAL_SNOOZES:
        raise ScheduleError(f"manual snoozes must be an integer in 0..{MAX_MANUAL_SNOOZES}")
    return value


def _validate(s: ScheduleSnapshot, first_lead_seconds: int) -> None:
    if not isinstance(s.id, str) or not _ID_RE.fullmatch(s.id):
        raise ScheduleError("id must be 32 lowercase hex characters")
    if s.kind not in _KINDS:
        raise ScheduleError(f"unknown kind: {s.kind!r}")
    if s.kind != "manual" and s.manual_snoozes is not None:
        raise ScheduleError("snoozes is only valid for kind 'manual'")
    if s.kind == "manual":
        count = validate_manual_snoozes(s.manual_snoozes)
        if s.wake_at is None or s.deadline_at is None:
            raise ScheduleError("kind 'manual' requires wake_at and deadline_at")
        if s.deadline_at.astimezone(UTC) - s.wake_at.astimezone(UTC) != timedelta(
            seconds=count * MANUAL_SNOOZE_SECONDS
        ):
            raise ScheduleError("manual wake must equal deadline minus snoozes * 390s")
    elif s.kind == "none":
        if s.wake_at is not None or s.deadline_at is not None or s.next_prep_at is not None:
            raise ScheduleError("kind 'none' requires null wake_at, deadline_at, next_prep_at")
    elif s.kind == "legacy":
        if s.wake_at is None or s.deadline_at is not None:
            raise ScheduleError("kind 'legacy' requires wake_at and null deadline_at")
    else:
        if s.wake_at is None or s.deadline_at is None:
            raise ScheduleError("kind 'commute' requires wake_at and deadline_at")
        lead = s.deadline_at.astimezone(UTC) - s.wake_at.astimezone(UTC)
        if lead != timedelta(seconds=first_lead_seconds):
            raise ScheduleError(
                f"commute deadline_at - wake_at must be {first_lead_seconds}s, "
                f"got {lead.total_seconds():g}s"
            )


def serialize_schedule(
    schedule: ScheduleSnapshot, *, first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> str:
    """Validate and render the exact canonical snapshot string (≤255 chars)."""
    _validate(schedule, first_lead_seconds)

    def ts(v: datetime | None) -> str | None:
        return format_utc(v) if v is not None else None

    body = {
        "v": SCHEDULE_VERSION,
        "id": schedule.id,
        "issued_at": format_utc(schedule.issued_at),
        "kind": schedule.kind,
        "wake_at": ts(schedule.wake_at),
        "deadline_at": ts(schedule.deadline_at),
        "next_prep_at": ts(schedule.next_prep_at),
    }
    if schedule.kind == "manual":
        body["snoozes"] = schedule.manual_snoozes
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=True)
    if len(raw) > MAX_SNAPSHOT_LENGTH:
        raise ScheduleError(f"snapshot is {len(raw)} chars (max {MAX_SNAPSHOT_LENGTH})")
    return raw


def new_schedule(
    *,
    issued_at: datetime,
    kind: ScheduleKind,
    wake_at: datetime | None,
    deadline_at: datetime | None,
    next_prep_at: datetime | None,
    schedule_id: str | None = None,
    manual_snoozes: int | None = None,
    first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> ScheduleSnapshot:
    """Build a validated snapshot with a fresh random id (unless given).

    Retries of one publication must reuse the returned object (same id and
    ``issued_at``) rather than calling this again.
    """
    def utc(v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ScheduleError("timestamps must be timezone-aware")
        return v.astimezone(UTC)

    issued = utc(issued_at)
    assert issued is not None
    snap = ScheduleSnapshot(
        id=schedule_id if schedule_id is not None else uuid.uuid4().hex,
        issued_at=issued,
        kind=kind,
        wake_at=utc(wake_at),
        deadline_at=utc(deadline_at),
        next_prep_at=utc(next_prep_at),
        manual_snoozes=manual_snoozes,
    )
    _validate(snap, first_lead_seconds)
    return snap


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise ScheduleError(f"duplicate key: {k!r}")
        out[k] = v
    return out


def _reject_constant(token: str) -> object:
    raise ScheduleError(f"non-standard JSON constant: {token}")


def parse_schedule(
    raw: object, first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> ScheduleSnapshot:
    """Parse the helper state into a :class:`ScheduleSnapshot`.

    ``raw`` must be the exact canonical string produced by
    :func:`serialize_schedule` (key order, compact separators, ASCII,
    canonical timestamps, ≤255 chars).  ``first_lead_seconds`` is the
    required ``deadline_at - wake_at`` for ``commute``.  Any deviation —
    including ``''``/``unknown``/``unavailable`` — raises
    :class:`ScheduleError`; callers must treat that as *no new information*.
    """
    if not isinstance(raw, str):
        raise ScheduleError("snapshot must be a string")
    if len(raw) > MAX_SNAPSHOT_LENGTH:
        raise ScheduleError("snapshot too long")
    try:
        data = json.loads(
            raw, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
        )
    except ScheduleError:
        raise
    except ValueError as exc:
        raise ScheduleError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ScheduleError("snapshot must be an object")
    keys = (*_KEYS, "snoozes") if data.get("kind") == "manual" else _KEYS
    if tuple(data) != keys:
        raise ScheduleError("snapshot keys must be exactly " + ",".join(keys))
    v = data["v"]
    if type(v) is not int or v != SCHEDULE_VERSION:
        raise ScheduleError(f"unsupported version: {v!r}")
    kind = data["kind"]
    if kind not in _KINDS:
        raise ScheduleError(f"unknown kind: {kind!r}")

    def opt(key: str) -> datetime | None:
        value = data[key]
        return None if value is None else parse_utc(value)

    ident = data["id"]
    if not isinstance(ident, str):
        raise ScheduleError("id must be a string")
    snap = ScheduleSnapshot(
        id=ident,
        issued_at=parse_utc(data["issued_at"]),
        kind=kind,
        wake_at=opt("wake_at"),
        deadline_at=opt("deadline_at"),
        next_prep_at=opt("next_prep_at"),
        manual_snoozes=validate_manual_snoozes(data["snoozes"]) if kind == "manual" else None,
    )
    if serialize_schedule(snap, first_lead_seconds=first_lead_seconds) != raw:
        raise ScheduleError("snapshot is not in canonical form")
    return snap


def compare_schedules(current: ScheduleSnapshot, incoming: ScheduleSnapshot) -> ScheduleOrder:
    """Order ``incoming`` against the committed ``current`` (mirrors the HA script).

    * ``same``     — same id and identical content (idempotent retry).
    * ``conflict`` — same id with different content, or different content
      at an equal ``issued_at`` instant.
    * ``stale``    — different id issued strictly earlier.
    * ``newer``    — different id issued strictly later (accept).

    Instants are compared as datetimes, never as ISO strings.
    """
    if incoming.id == current.id:
        return "same" if incoming == current else "conflict"
    a = current.issued_at.astimezone(UTC)
    b = incoming.issued_at.astimezone(UTC)
    if b == a:
        return "conflict"
    return "newer" if b > a else "stale"


def canonical_snapshot(
    schedule: ScheduleSnapshot, *, first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> str:
    """Alias of :func:`serialize_schedule`: the exact helper/script string."""
    return serialize_schedule(schedule, first_lead_seconds=first_lead_seconds)


PUBLISH_PAYLOAD_KEYS = frozenset({"snapshot", "next_event"})


def build_publish_payload(
    schedule: ScheduleSnapshot,
    next_event: str = "",
    *,
    first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> dict[str, str]:
    """Service data for ``publish_v1_script``: ``{"snapshot": str, "next_event": str}``.

    ``snapshot`` is a JSON *string*, never a mapping; ``next_event`` is
    display-only and deliberately outside the snapshot.
    """
    return {
        "snapshot": serialize_schedule(schedule, first_lead_seconds=first_lead_seconds),
        "next_event": next_event,
    }


def validate_publish_payload(
    data: object, *, first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> ScheduleSnapshot:
    """Validate v1 script service data as the HA script does; return the snapshot.

    Only ``snapshot`` is authoritative: it is required and must be the
    canonical string (see :func:`parse_schedule`).  ``next_event`` is
    display-only and, like any other field, is not validated — an HA script
    cannot see undeclared service variables, so rejecting unknown fields
    would be a check HA could never perform.  The publisher itself always
    sends exactly :data:`PUBLISH_PAYLOAD_KEYS`.  Raises :class:`ScheduleError`.
    """
    if not isinstance(data, dict):
        raise ScheduleError("payload must be a mapping")
    if "snapshot" not in data:
        raise ScheduleError("payload requires snapshot")
    return parse_schedule(data["snapshot"], first_lead_seconds)


CommitDecision = Literal["commit", "already_committed", "invalid", "stale_or_conflict"]
_EMPTY_HELPER_STATES = frozenset({"", "unknown", "unavailable"})


def decide_commit(
    current_raw: object,
    incoming_raw: object,
    *,
    first_lead_seconds: int = DEFAULT_FIRST_LEAD_SECONDS,
) -> CommitDecision:
    """Model of the documented HA publish script's single-write decision.

    * ``invalid``           — incoming fails :func:`parse_schedule` (no write).
    * ``already_committed`` — helper already holds the exact same string.
    * ``commit``            — helper is ``''``/``unknown``/``unavailable``,
      or the incoming id differs and ``issued_at`` is strictly later.
    * ``stale_or_conflict`` — everything else: helper content not a valid
      snapshot, same id with a different body, or ``issued_at`` equal/earlier.

    ``issued_at`` is compared as an absolute UTC instant (never as a string),
    so ordering is deterministic.  Like the documented script, which runs the
    full validity check on a nonempty helper value too, any non-canonical
    current value refuses, requiring an explicit operator reset — fail closed
    rather than overwrite unknown state.
    """
    try:
        incoming = parse_schedule(incoming_raw, first_lead_seconds)
    except ScheduleError:
        return "invalid"
    if current_raw == incoming_raw:
        return "already_committed"
    if isinstance(current_raw, str) and current_raw in _EMPTY_HELPER_STATES:
        return "commit"
    try:
        current = parse_schedule(current_raw, first_lead_seconds)
    except ScheduleError:
        return "stale_or_conflict"
    return "commit" if compare_schedules(current, incoming) == "newer" else "stale_or_conflict"


def status_freshness_budget(poll_seconds: int) -> timedelta:
    """Freshness budget for status ``observed_at``: ``max(90s, 3 * poll)``."""
    return timedelta(seconds=max(STATUS_MIN_FRESHNESS_SECONDS, 3 * poll_seconds))


def status_observation_fresh(observed_at: object, now: datetime, poll_seconds: int) -> bool:
    """Whether a status record's ``observed_at`` is usable at ``now``.

    Conservative: returns False for a non-canonical/unparseable value, a
    naive ``now``, an ``observed_at`` in the future, or one older than
    :func:`status_freshness_budget`.  The status *state* (e.g. ``ringing``)
    never substitutes for this check.

    Consumers must call this on **every** use; an earlier "fresh" result is
    never cached or carried forward.  There is deliberately no deadline
    parameter: a final may be valid indefinitely beyond the deadline only
    while ``observed_at`` keeps refreshing within the budget and this check
    is made on every use.  Once stale, any record — including a
    terminal one — may be shown as history but must never authorize an
    action.  The L+10 recovery cutoff is not a status expiry.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        return False
    try:
        observed = parse_utc(observed_at)
    except ScheduleError:
        return False
    age = now.astimezone(UTC) - observed
    return timedelta(0) <= age <= status_freshness_budget(poll_seconds)
