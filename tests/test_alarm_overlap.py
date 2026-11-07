"""Sanitized live regression for timed Google Calendar overlap snapshots."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_alarm import _config, _nyc, _plan, _provider

from commutecompass.alarm import compute_wake, wake_for_day


@pytest.mark.parametrize("utc_inputs", [False, True])
@pytest.mark.parametrize("now", [
    _nyc(2026, 11, 4, 18, 55),
    _nyc(2026, 11, 5, 0, 15),
    _nyc(2026, 11, 5, 8),
    _nyc(2026, 11, 5, 20),
])
def test_live_shaped_timed_multiday_google_overlap(
    tmp_path: Path, now: datetime, utc_inputs: bool,
) -> None:
    """Sanitized live failure: fresh overlapping snapshots, NOT cached plans.

    Google includes a timed Nov 4–6 event in every overlapping query. Its
    too-imminent Nov 4 prep used to poison all three days (the started-event
    branch also substituted its old start). Titles/identities are synthetic;
    event spans and observed prep/leave times preserve the production shape.
    """
    cfg = _config(tmp_path)
    cfg.alarm.window.enabled = True
    cfg.alarm.window.first_alarm_lead_minutes = 60

    def stamp(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(UTC) if utc_inputs else dt

    overlap = _plan(stamp("2026-11-04T18:15:49-05:00"),
                    "Sanitized multi-day gathering", error="too_imminent")
    overlap.event.id = "sanitized-timed-multiday"
    overlap.event.start = stamp("2026-11-04T19:30:00-05:00")
    overlap.event.end = stamp("2026-11-06T21:30:00-05:00")
    overlap.leave_at = stamp("2026-11-04T18:45:49-05:00")
    previous_morning = _plan(stamp("2026-11-04T09:00:32-05:00"),
                             "Sanitized prior morning event", error="too_imminent")
    previous_morning.event.id = "sanitized-prior-morning"
    previous_morning.event.start = stamp("2026-11-04T10:00:00-05:00")
    previous_morning.event.end = stamp("2026-11-04T11:00:00-05:00")
    previous_morning.leave_at = stamp("2026-11-04T09:30:32-05:00")
    shift = _plan(stamp("2026-11-04T18:14:49-05:00"),
                  "Sanitized evening shift", error="too_imminent")
    shift.event.id = "sanitized-evening-shift"
    shift.event.start = stamp("2026-11-04T19:00:00-05:00")
    shift.event.end = stamp("2026-11-05T01:00:00-05:00")
    shift.leave_at = stamp("2026-11-04T18:44:49-05:00")
    morning = _plan(stamp("2026-11-06T09:00:32-05:00"), "Sanitized morning event")
    morning.event.start = stamp("2026-11-06T10:00:00-05:00")
    morning.event.end = stamp("2026-11-06T11:00:00-05:00")
    morning.leave_at = stamp("2026-11-06T09:30:32-05:00")
    evening = _plan(stamp("2026-11-06T18:50:26-05:00"), "Sanitized evening event")
    evening.event.start = stamp("2026-11-06T20:00:00-05:00")
    evening.event.end = stamp("2026-11-06T21:00:00-05:00")
    evening.leave_at = stamp("2026-11-06T19:20:26-05:00")
    provider = _provider({
        date(2026, 11, 4): [previous_morning, overlap, shift],
        date(2026, 11, 5): [overlap],
        date(2026, 11, 6): [overlap, morning, evening],
    })

    result = compute_wake(cfg, now, provider)
    expected = (_nyc(2026, 11, 5, 11) if now.astimezone(UTC)
                < _nyc(2026, 11, 5, 11).astimezone(UTC)
                else stamp("2026-11-06T08:30:32-05:00"))
    assert result.wake_at.astimezone(UTC) == expected.astimezone(UTC)
    assert len(provider.calls) == 2
    fifth = _nyc(2026, 11, 5, 2)
    sixth = _nyc(2026, 11, 6, 2)
    assert wake_for_day(fifth, [overlap], cfg, now=now) == (_nyc(2026, 11, 5, 11), None)
    sixth_wake, sixth_plan = wake_for_day(sixth, [overlap, morning, evening], cfg, now=now)
    assert sixth_wake.astimezone(UTC) == stamp("2026-11-06T08:30:32-05:00").astimezone(UTC)
    assert sixth_plan is morning
