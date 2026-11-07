"""Durable one-shot manual wake intent and last successful alarm publication.

Deadlines are departure/ready-by instants, not wake instants. Only a caller
selecting a real schedule should bind intent; merely computing a preview must
not bind it. All persisted identities and comparisons use UTC instants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from commutecompass.alarm_contract import validate_manual_snoozes
from commutecompass.config import Config
from commutecompass.store import Store
from commutecompass.timeutil import NYC_TZ


@dataclass(frozen=True)
class ManualWake:
    deadline_at: datetime
    force: bool = False
    manual_snoozes: int | None = None

    # A loaded intent carries its durable identity without changing value equality.
    _version: int | None = field(default=None, repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.manual_snoozes is not None:
            validate_manual_snoozes(self.manual_snoozes)


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("alarm timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _store(config: Config, *, initialize: bool = True) -> Store:
    store = Store(config.paths.db_path)
    if initialize:
        store.init_schema()
    return store


def load_manual(config: Config, now: datetime, *, dry_run: bool = False) -> ManualWake | None:
    """Load live intent, expiring deadline <= now (without writes in dry-run)."""
    _aware(now)
    value = _store(config, initialize=not dry_run).load_alarm_manual(now, dry_run=dry_run)
    return ManualWake(value[0].astimezone(NYC_TZ), value[1], value[3], _version=value[2]) if value else None


def save_manual(config: Config, manual: ManualWake) -> None:
    """Replace intent, invalidating any previous wake binding even for equal values."""
    _aware(manual.deadline_at)
    _store(config).save_alarm_manual(manual.deadline_at, manual.force, manual.manual_snoozes)


def clear_manual(config: Config) -> None:
    """Forget manual intent, leaving publication history intact."""
    _store(config).clear_alarm_manual()


def load_last_publish(config: Config) -> dict[str, object] | None:
    """Load the last successful caller-serialized result without writing anything."""
    return _store(config, initialize=False).load_alarm_last_publish()


def save_last_publish(config: Config, payload: dict[str, object]) -> None:
    """Record a successful publish; callers must not call this after a failure."""
    _store(config).save_alarm_last_publish(payload)


def bind_manual(
    config: Config,
    wake_at: datetime,
    deadline_at: datetime,
    *,
    expected_manual: ManualWake | None = None,
) -> None:
    """Bind a selected schedule only if the observed manual intent is still current.

    Pass the exact ``load_manual`` result observed before publishing as
    ``expected_manual``. Its version rejects even identical replacements during
    publication. ``deadline_at`` may be an earlier calendar deadline on the
    manual's logical day; the wake itself may cross that day's boundary.

    Legacy callers without a loaded identity can only bind an exact matching
    deadline. They cannot detect an identical replacement during publication.
    A supplied but unversioned ManualWake is not a valid identity and cannot bind.
    """
    _aware(wake_at)
    _aware(deadline_at)
    if expected_manual is not None and expected_manual._version is None:
        return
    _store(config).bind_alarm_manual(
        wake_at,
        deadline_at,
        expected_deadline_at=expected_manual.deadline_at if expected_manual else None,
        expected_version=expected_manual._version if expected_manual else None,
    )


def clear_manual_for_wake(config: Config, wake_at: datetime) -> bool:
    """Consume only intent still bound to this completed absolute wake instant."""
    _aware(wake_at)
    return _store(config).clear_alarm_manual_for_wake(wake_at)


def _wall_time(value: str) -> time:
    value = value.strip().lower()
    match = re.fullmatch(r"([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)", value)
    if match:
        hour, minute, period = match.groups()
        if not 1 <= int(hour) <= 12:
            raise ValueError("12-hour clock hour must be between 1 and 12")
        return time(int(hour) % 12 + (12 if period == "pm" else 0), int(minute or 0))
    if re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        return time.fromisoformat(value)
    raise ValueError("manual deadline must be HH:MM or a time such as 9am/9:30am")


def _occurrences(day: date, wall: time) -> list[datetime]:
    """Validate a local wall time by UTC round-trip; preserve both fold instants."""
    naive = datetime.combine(day, wall)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=NYC_TZ, fold=fold)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(NYC_TZ).replace(tzinfo=None) == naive:
            candidates[instant] = candidate
    if not candidates:
        raise ValueError("manual deadline falls in an America/New_York DST gap")
    return [candidates[instant] for instant in sorted(candidates)]


def parse_manual_deadline(value: str, date_value: date | None, now: datetime) -> datetime:
    """Parse a NYC wall time into a strictly future departure deadline.

    With no date, select today's next absolute occurrence (including the second
    fall-back occurrence), otherwise tomorrow's. Explicit dates never roll
    forward and select the earliest still-future fold occurrence. DST gaps are
    rejected rather than silently normalized. Dates here are civil dates;
    logical planning dates are derived from the resulting instant when binding.
    """
    now_utc = _aware(now)
    wall = _wall_time(value)
    day = date_value if date_value is not None else now.astimezone(NYC_TZ).date()
    for candidate in _occurrences(day, wall):
        if candidate.astimezone(UTC) > now_utc:
            return candidate
    if date_value is not None:
        raise ValueError("manual deadline must be in the future")
    return _occurrences(day + timedelta(days=1), wall)[0]
