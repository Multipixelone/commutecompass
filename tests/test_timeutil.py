"""Tests for timeutil.py — NYC timezone helpers."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from commutecompass.timeutil import (
    DAY_START_HOUR,
    NYC_TZ,
    is_within_quiet_hours,
    logical_day_bounds_nyc,
    now_nyc,
    parse_iso_nyc,
    to_nyc,
)

UTC = timezone.utc


class TestNowNyc:
    """Test now_nyc()."""

    def test_returns_aware_datetime(self) -> None:
        result = now_nyc()
        assert result.tzinfo is not None
        assert result.tzinfo == NYC_TZ

    def test_return_type_is_datetime(self) -> None:
        result = now_nyc()
        assert isinstance(result, datetime)


class TestToNyc:
    """Test to_nyc()."""

    def test_converts_naive_to_nyc(self) -> None:
        naive = datetime(2025, 3, 15, 10, 30, 0)
        result = to_nyc(naive)
        assert result.tzinfo == NYC_TZ

    def test_converts_aware_to_nyc(self) -> None:
        utc_dt = datetime(2025, 3, 15, 14, 30, 0, tzinfo=datetime.now().astimezone(UTC).tzinfo)
        # Convert from a different zone
        other_tz = UTC
        aware_other = datetime(2025, 3, 15, 9, 30, 0, tzinfo=other_tz)
        result = to_nyc(aware_other)
        assert result.tzinfo == NYC_TZ

    def test_preserves_est_offset(self) -> None:
        # January 2025: NYC is firmly in EST (UTC-5)
        # to_nyc on a naive datetime attaches NYC_TZ
        naive = datetime(2025, 1, 15, 10, 0, 0)  # winter, definitely EST
        result = to_nyc(naive)
        assert result.utcoffset() == timedelta(hours=-5)

    def test_idempotent_already_nyc(self) -> None:
        aware = datetime(2025, 3, 15, 10, 30, 0, tzinfo=NYC_TZ)
        result = to_nyc(aware)
        assert result == aware


class TestParseIsoNyc:
    """Test parse_iso_nyc()."""

    def test_parses_naive_iso(self) -> None:
        result = parse_iso_nyc("2025-03-15T10:30:00")
        assert result.tzinfo == NYC_TZ

    def test_parses_aware_iso_with_offset(self) -> None:
        result = parse_iso_nyc("2025-03-15T10:30:00-05:00")
        assert result.tzinfo == NYC_TZ

    def test_parses_iso_with_z_suffix(self) -> None:
        result = parse_iso_nyc("2025-03-15T15:30:00Z")
        assert result.tzinfo == NYC_TZ


class TestIsWithinQuietHours:
    """Test is_within_quiet_hours() edge cases."""

    # ─── Same-day windows ───────────────────────────────────────────────

    def test_within_same_day_window(self) -> None:
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True

    def test_outside_same_day_window(self) -> None:
        dt = datetime(2025, 6, 1, 7, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is False

    def test_at_same_day_start_boundary(self) -> None:
        dt = datetime(2025, 6, 1, 9, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True

    def test_at_same_day_end_boundary(self) -> None:
        dt = datetime(2025, 6, 1, 17, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True

    # ─── Overnight windows ──────────────────────────────────────────────

    def test_within_overnight_window_late_night(self) -> None:
        dt = datetime(2025, 6, 1, 23, 30, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is True

    def test_within_overnight_window_early_morning(self) -> None:
        dt = datetime(2025, 6, 1, 5, 30, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is True

    def test_outside_overnight_window_daytime(self) -> None:
        dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is False

    def test_outside_overnight_window_evening(self) -> None:
        dt = datetime(2025, 6, 1, 20, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is False

    def test_at_overnight_start_boundary(self) -> None:
        dt = datetime(2025, 6, 1, 22, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is True

    def test_at_overnight_end_boundary(self) -> None:
        dt = datetime(2025, 6, 1, 7, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is True

    # ─── Naive datetime input ──────────────────────────────────────────

    def test_naive_dt_converted_to_nyc(self) -> None:
        # Naive datetime should be treated as NYC local time
        naive = datetime(2025, 6, 1, 23, 30, 0)
        # When start=22:00, end=07:00, 23:30 is within overnight window
        assert is_within_quiet_hours(naive, time(22, 0), time(7, 0)) is True

    def test_naive_dt_outside_overnight(self) -> None:
        naive = datetime(2025, 6, 1, 12, 0, 0)
        assert is_within_quiet_hours(naive, time(22, 0), time(7, 0)) is False

    # ─── DST boundary cases ─────────────────────────────────────────────

    def test_dst_spring_forward_early_morning(self) -> None:
        # DST starts Sun March 9 2025 at 2:00 AM (clocks skip to 3:00 AM).
        # On the night of the transition, 2:00 AM doesn't exist.
        # 3:00 AM EDT is equivalent to 2:00 AM EST.
        dt_spring = datetime(2025, 3, 9, 3, 30, 0, tzinfo=NYC_TZ)
        # Quiet hours 01:00–06:00: 3:30 is in window
        assert is_within_quiet_hours(dt_spring, time(1, 0), time(6, 0)) is True

    def test_dst_spring_forward_edge_case_before_transition(self) -> None:
        # Before 3:00 AM EDT — technically 2:XX AM EST (which doesn't exist in wall time)
        # Python's datetime folds the non-existent 2:XX to 3:XX EDT automatically.
        # Test that 01:30 on March 9 is in quiet hours (01:00–06:00)
        dt_before = datetime(2025, 3, 9, 1, 30, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt_before, time(1, 0), time(6, 0)) is True

    def test_dst_fall_back_evening(self) -> None:
        # DST ends Sun November 2 2025 at 2:00 AM (clocks fall back to 1:00 AM).
        # After the fall-back, 1:00–2:00 AM happens twice (EDT then EST).
        # Test that 1:30 AM on Nov 2 is in quiet hours (22:00–07:00) — overnight window.
        dt_before_fall = datetime(2025, 11, 2, 1, 30, 0, tzinfo=NYC_TZ)
        # 1:30 is within 22:00–07:00 overnight window
        assert is_within_quiet_hours(dt_before_fall, time(22, 0), time(7, 0)) is True

    def test_dst_fall_back_2am_handling(self) -> None:
        # 2:30 AM doesn't exist on DST fall-back night — Python folds to 1:30 AM EST.
        # But we test the boundary: 2:00 AM never occurs (it's the "gap" hour).
        dt_gap = datetime(2025, 11, 2, 2, 30, 0, tzinfo=NYC_TZ)
        # In Python, 2:30 AM on the DST fall-back night is folded to 1:30 AM EST.
        # 1:30 AM EST is still within 22:00–07:00 quiet hours
        assert is_within_quiet_hours(dt_gap, time(22, 0), time(7, 0)) is True

    def test_overnight_window_spanning_dst_transition(self) -> None:
        # Quiet hours 22:00–07:00 on DST changeover night
        # March 9: 22:00–07:00 spans the DST transition (3 AM gap)
        dt_late_evening = datetime(2025, 3, 9, 23, 0, 0, tzinfo=NYC_TZ)
        dt_early_morning = datetime(2025, 3, 10, 5, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt_late_evening, time(22, 0), time(7, 0)) is True
        assert is_within_quiet_hours(dt_early_morning, time(22, 0), time(7, 0)) is True

    def test_normal_edt_summer_daytime(self) -> None:
        # Summer: NYC is EDT (UTC-4)
        dt = datetime(2025, 7, 15, 14, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is False

    def test_normal_est_winter(self) -> None:
        # Winter: NYC is EST (UTC-5)
        dt = datetime(2025, 1, 15, 14, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is False

    # ─── Minute-level precision ────────────────────────────────────────

    def test_same_day_exact_minute(self) -> None:
        dt = datetime(2025, 6, 1, 9, 0, 1, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(9, 0), time(17, 0)) is True

    def test_overnight_exact_minute_start(self) -> None:
        dt = datetime(2025, 6, 1, 22, 0, 1, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is True

    def test_overnight_exact_minute_end(self) -> None:
        # 7:00:01 is AFTER the 07:00 end boundary — not quiet hours
        dt = datetime(2025, 6, 1, 7, 0, 1, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt, time(22, 0), time(7, 0)) is False

    # ─── Midnight boundary ─────────────────────────────────────────────

    def test_exactly_midnight_within_overnight(self) -> None:
        # With window 23:00–06:00: 0:00 is AFTER start (23:00) — wait, no.
        # 0:00 >= 23:00? No. 0:00 <= 06:00? Yes → True
        dt_midnight = datetime(2025, 6, 1, 0, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt_midnight, time(23, 0), time(6, 0)) is True

    def test_exactly_midnight_overnight_window_end(self) -> None:
        # With 22:00–07:00: midnight is NOT covered (0:00 < 7:00, need to be >= start OR <= end)
        # 0:00 >= 22:00? No. 0:00 <= 07:00? Yes → True
        dt_midnight = datetime(2025, 6, 1, 0, 0, 0, tzinfo=NYC_TZ)
        assert is_within_quiet_hours(dt_midnight, time(22, 0), time(7, 0)) is True


class TestToNycDST:
    """Verify to_nyc handles DST correctly."""

    def test_spring_dst_offset_change(self) -> None:
        # March 9 2025 1:00 AM EST → March 9 2025 3:00 AM EDT
        before = datetime(2025, 3, 9, 1, 0, 0, tzinfo=NYC_TZ)
        after = datetime(2025, 3, 9, 3, 0, 0, tzinfo=NYC_TZ)
        # 3 AM EDT = 2 AM EST folded; the UTC offsets differ by 1 hour
        assert before.utcoffset() == timedelta(hours=-5)
        assert after.utcoffset() == timedelta(hours=-4)

    def test_fall_dst_offset_change(self) -> None:
        # November 2 2025: DST ends at 2:00 AM (clocks fall back to 1:00 AM).
        # 1:00 AM with fold=0 is still DST-on (EDT, UTC-4).
        # 1:00 AM with fold=1 is DST-off (EST, UTC-5).
        # 2:00 AM on the fall-back morning has fold=1 and is EST (UTC-5).
        before_fold0 = datetime(2025, 11, 2, 1, 0, 0, tzinfo=NYC_TZ, fold=0)
        before_fold1 = datetime(2025, 11, 2, 1, 0, 0, tzinfo=NYC_TZ, fold=1)
        assert before_fold0.utcoffset() == timedelta(hours=-4)  # EDT
        assert before_fold1.utcoffset() == timedelta(hours=-5)  # EST
        # 2:00 AM on fall-back morning (after the transition) is EST
        after = datetime(2025, 11, 2, 2, 0, 0, tzinfo=NYC_TZ)
        assert after.utcoffset() == timedelta(hours=-5)  # EST


class TestLogicalDayBoundsNyc:
    """Test logical_day_bounds_nyc() with 2AM day start."""

    def test_reference_before_day_start_maps_to_previous_logical_day(self) -> None:
        ref = datetime(2026, 5, 12, 1, 30, 0, tzinfo=NYC_TZ)
        start, end = logical_day_bounds_nyc(ref)

        assert start == datetime(2026, 5, 11, DAY_START_HOUR, 0, 0, tzinfo=NYC_TZ)
        assert end == datetime(2026, 5, 12, 1, 59, 59, 999999, tzinfo=NYC_TZ)

    def test_reference_after_day_start_maps_to_same_calendar_day(self) -> None:
        ref = datetime(2026, 5, 12, 9, 0, 0, tzinfo=NYC_TZ)
        start, end = logical_day_bounds_nyc(ref)

        assert start == datetime(2026, 5, 12, DAY_START_HOUR, 0, 0, tzinfo=NYC_TZ)
        assert end == datetime(2026, 5, 13, 1, 59, 59, 999999, tzinfo=NYC_TZ)

    def test_invalid_day_start_raises(self) -> None:
        with pytest.raises(ValueError):
            logical_day_bounds_nyc(datetime(2026, 5, 12, 9, 0, 0, tzinfo=NYC_TZ), day_start_hour=24)
