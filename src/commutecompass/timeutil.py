"""NYC timezone helpers."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from zoneinfo import ZoneInfo


NYC_TZ = ZoneInfo("America/New_York")

# Business day boundary for commute planning.
# Events between 00:00-01:59 local NYC are treated as part of the previous day.
DAY_START_HOUR = 2


def now_nyc() -> datetime:
    """Return current datetime in NYC timezone."""
    return datetime.now(NYC_TZ)


def to_nyc(dt: datetime) -> datetime:
    """Convert a datetime to NYC timezone (normalize if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NYC_TZ)
    return dt.astimezone(NYC_TZ)


def parse_iso_nyc(s: str) -> datetime:
    """Parse an ISO-8601 string and return it in NYC timezone."""
    dt = datetime.fromisoformat(s)
    return to_nyc(dt)


def logical_day_bounds_nyc(
    reference: datetime | None = None,
    *,
    day_start_hour: int = DAY_START_HOUR,
) -> tuple[datetime, datetime]:
    """Return NYC logical day bounds [start, end] for a reference time.

    The logical day starts at ``day_start_hour`` in NYC local time, not midnight.
    Example with ``day_start_hour=2``:
    - reference 2026-05-12 01:30 => day is 2026-05-11 02:00 to 2026-05-12 01:59:59.999999
    - reference 2026-05-12 09:00 => day is 2026-05-12 02:00 to 2026-05-13 01:59:59.999999

    Args:
        reference: Datetime used to choose the logical day (defaults to now in NYC).
        day_start_hour: Local NYC hour (0-23) at which the logical day begins.

    Returns:
        Tuple of (start_inclusive, end_inclusive), both timezone-aware NYC datetimes.
    """
    if not 0 <= day_start_hour <= 23:
        raise ValueError("day_start_hour must be between 0 and 23")

    ref = now_nyc() if reference is None else to_nyc(reference)
    start = ref.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    if ref < start:
        start = start - timedelta(days=1)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def is_within_quiet_hours(dt: datetime, start: time, end: time) -> bool:
    """Return True if time dt falls within a quiet-hours window spanning midnight.

    Handles overnight windows (e.g., 22:00–07:00) where start > end.
    Times are compared in NYC timezone; dt is converted to aware NYC datetime
    before extracting its time component.

    Args:
        dt: Any datetime (naive or aware). Compared against the time component in NYC.
        start: Quiet-hours start time in NYC (naive time, treated as local NYC).
        end: Quiet-hours end time in NYC (naive time, treated as local NYC).

    Returns:
        True if the NYC time of dt falls within [start, end]. For overnight windows
        (start > end), returns True when time >= start OR time <= end.
    """
    # Normalize dt to aware NYC datetime, extract time component
    aware_dt = to_nyc(dt)
    dt_time = aware_dt.timetz()

    # Make start/end tz-aware for comparison by attaching NYC tzinfo.
    # Naive time + UTC offset equivalent to NYC = still naive but comparable
    # when both sides are treated consistently. Since we only compare
    # time components (hour/minute/second), we normalize to naive by
    # replacing tzinfo on the time objects directly so comparisons work
    # across DST boundaries (times shift but the comparison logic holds).
    #
    # Approach: convert both times to naive wall-clock values by using
    # timetz() which preserves wall-clock values. We then compare naive
    # times directly — this is valid because both start/end are local NYC
    # times and dt_time is the local NYC wall-clock time.
    #
    # For Python 3.12+ type correctness, compare as naive times:
    naive_start = time(start.hour, start.minute, start.second, start.microsecond)
    naive_end = time(end.hour, end.minute, end.second, end.microsecond)
    naive_dt_time = time(dt_time.hour, dt_time.minute, dt_time.second, dt_time.microsecond)

    if naive_start <= naive_end:
        # Same-day window: dt must fall between start and end (inclusive)
        return naive_start <= naive_dt_time <= naive_end
    else:
        # Overnight window (spans midnight): dt is in window if >= start OR <= end
        return naive_dt_time >= naive_start or naive_dt_time <= naive_end
