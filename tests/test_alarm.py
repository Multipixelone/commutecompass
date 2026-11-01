"""Tests for alarm.py — wake computation + publish payload."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from commutecompass.alarm import (
    AlarmConfigError,
    build_payload,
    compute_wake,
    publish,
)
from commutecompass.config import (
    AlarmConfig,
    CalendarSpec,
    Config,
    HomeAssistantConfig,
    MtaConfig,
    OpencodeGoConfig,
    Origin,
    PathsConfig,
    PrepConfig,
    SchedulingConfig,
)
from commutecompass.models import Event, Plan
from commutecompass.timeutil import NYC_TZ


def _config(tmp_path: Path, **alarm: Any) -> Config:
    return Config(
        origin=Origin(address="123 Example Ave", lat=40.69, lon=-73.98),
        calendars=[CalendarSpec(id="c", name="C")],
        prep=PrepConfig(),
        scheduling=SchedulingConfig(),
        paths=PathsConfig(
            venues_file=str(tmp_path / "v.yaml"),
            db_path=str(tmp_path / "t.db"),
            oauth_token_path=str(tmp_path / "tok.json"),
        ),
        opencode_go=OpencodeGoConfig(endpoint="https://x"),
        mta=MtaConfig(subway_alerts_url="https://x", lirr_alerts_url="https://x", bus_alerts_url="https://x"),
        home_assistant=HomeAssistantConfig(enabled=True, base_url="http://ha.local:8123"),
        alarm=AlarmConfig(enabled=True, **alarm),
        home_assistant_token="ha-tok",
    )


def _plan(prep_at: datetime, title: str = "Class", *, error: str | None = None) -> Plan:
    ev = Event(
        id=f"e-{prep_at.isoformat()}",
        calendar_id="c",
        calendar_name="C",
        title=title,
        start=prep_at + timedelta(hours=1),
        end=prep_at + timedelta(hours=2),
        location_raw="x",
    )
    if error:
        return Plan(event=ev, error=error)
    return Plan(event=ev, prep_at=prep_at, leave_at=prep_at + timedelta(minutes=20))


def _nyc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NYC_TZ)


def _provider(by_day: dict[date, list[Plan]]) -> Any:
    calls: list[date] = []

    def provider(day_start: datetime) -> list[Plan]:
        d = day_start.astimezone(NYC_TZ).date()
        calls.append(d)
        return by_day.get(d, [])

    provider.calls = calls  # type: ignore[attr-defined]
    return provider


def test_event_before_noon_wakes_at_prep(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    prep = _nyc(2026, 10, 15, 8, 30)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [_plan(prep)]}))
    assert r.wake_at == prep
    assert r.source == "event"
    assert r.day == date(2026, 10, 15)
    assert r.next_prep_at == prep
    assert r.next_event == "Class"


def test_event_after_noon_caps_at_fallback(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    prep = _nyc(2026, 10, 15, 14, 0)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [_plan(prep)]}))
    assert r.wake_at == _nyc(2026, 10, 15, 12, 0)
    assert r.source == "fallback"
    # Nap cap still sees the afternoon prep.
    assert r.next_prep_at == prep


def test_no_events_uses_fallback(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({}))
    assert r.wake_at == _nyc(2026, 10, 15, 12, 0)
    assert r.source == "fallback"
    assert r.next_prep_at is None
    assert r.next_event == ""


def test_errored_plans_ignored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plans = [_plan(_nyc(2026, 10, 15, 7, 0), error="location_unresolved")]
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): plans}))
    assert r.source == "fallback"


def test_custom_fallback_time(tmp_path: Path) -> None:
    cfg = _config(tmp_path, fallback_time="10:15")
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({}))
    assert r.wake_at == _nyc(2026, 10, 15, 10, 15)


def test_wake_lead_subtracted(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=45)
    prep = _nyc(2026, 10, 15, 9, 0)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [_plan(prep)]}))
    assert r.wake_at == _nyc(2026, 10, 15, 8, 15)
    assert r.next_prep_at == prep


def test_earliest_of_multiple_events(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plans = [_plan(_nyc(2026, 10, 15, 10, 0), "B"), _plan(_nyc(2026, 10, 15, 7, 45), "A")]
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): plans}))
    assert r.wake_at == _nyc(2026, 10, 15, 7, 45)
    assert r.next_event == "A"


def test_passed_wake_rolls_to_tomorrow(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    tomorrow_prep = _nyc(2026, 10, 16, 9, 0)
    prov = _provider({date(2026, 10, 16): [_plan(tomorrow_prep)]})
    r = compute_wake(cfg, _nyc(2026, 10, 15, 13, 0), prov)
    assert r.day == date(2026, 10, 16)
    assert r.wake_at == tomorrow_prep
    assert prov.calls == [date(2026, 10, 15), date(2026, 10, 16)]


def test_tomorrow_not_planned_when_today_future(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    prov = _provider({})
    compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), prov)
    assert prov.calls == [date(2026, 10, 15)]


def test_after_midnight_belongs_to_previous_logical_day(tmp_path: Path) -> None:
    """01:00 is still the previous logical day; its noon has passed → next day."""
    cfg = _config(tmp_path)
    r = compute_wake(cfg, _nyc(2026, 10, 16, 1, 0), _provider({}))
    assert r.day == date(2026, 10, 16)
    assert r.wake_at == _nyc(2026, 10, 16, 12, 0)


def test_next_prep_includes_rest_of_today_when_rolling(tmp_path: Path) -> None:
    """Evening event today is the nap cap even though wake targets tomorrow."""
    cfg = _config(tmp_path)
    today_evening = _nyc(2026, 10, 15, 18, 0)
    prov = _provider({date(2026, 10, 15): [_plan(today_evening, "Show")]})
    r = compute_wake(cfg, _nyc(2026, 10, 15, 13, 0), prov)
    assert r.day == date(2026, 10, 16)
    assert r.next_prep_at == today_evening
    assert r.next_event == "Show"


def test_dst_fall_back_day_nov_1_2026(tmp_path: Path) -> None:
    """Nov 1 2026 01:59 EDT → 01:00 EST. Wake uses EST offsets and absolute lead."""
    cfg = _config(tmp_path, wake_lead_minutes=60)
    prep = _nyc(2026, 11, 1, 9, 0)  # EST
    prov = _provider({date(2026, 11, 1): [_plan(prep)]})
    # 23:00 EDT on Oct 31 — today's noon passed, so target Nov 1.
    r = compute_wake(cfg, _nyc(2026, 10, 31, 23, 0), prov)
    assert r.day == date(2026, 11, 1)
    assert r.wake_at == _nyc(2026, 11, 1, 8, 0)
    assert r.wake_at.utcoffset() == timedelta(hours=-5)
    assert r.wake_at.astimezone(UTC) == prep.astimezone(UTC) - timedelta(minutes=60)


def test_dst_fallback_noon_is_est(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    # 01:30 on Nov 1 (ambiguous hour) is still Oct 31's logical day.
    r = compute_wake(cfg, _nyc(2026, 11, 1, 1, 30), _provider({}))
    assert r.day == date(2026, 11, 1)
    assert r.wake_at.astimezone(UTC) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)


def test_dst_lead_across_transition_is_absolute(tmp_path: Path) -> None:
    """A lead spanning the repeated hour subtracts real minutes, not wall-clock."""
    cfg = _config(tmp_path, wake_lead_minutes=120)
    prep = _nyc(2026, 11, 1, 3, 0)  # 08:00 UTC
    prov = _provider({date(2026, 11, 1): [_plan(prep)]})
    r = compute_wake(cfg, _nyc(2026, 10, 31, 23, 0), prov)
    assert r.wake_at.astimezone(UTC) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def test_title_sanitized(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    prep = _nyc(2026, 10, 15, 8, 0)
    prov = _provider({date(2026, 10, 15): [_plan(prep, "Bad\u202etitle\nhere")]})
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), prov)
    assert r.next_event == "Badtitle here"


# ─────────── publish ───────────────────────────────────────────────────────────


def test_publish_calls_ha_script(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    prep = _nyc(2026, 10, 15, 8, 0)
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        result, ok = publish(
            cfg,
            now=_nyc(2026, 10, 15, 5, 0),
            plans_for_day=_provider({date(2026, 10, 15): [_plan(prep)]}),
        )
    assert ok
    args, kwargs = cs.call_args
    assert args[:4] == ("http://ha.local:8123", "ha-tok", "script", "commute_publish_alarm")
    assert kwargs["data"] == {
        "wake_at": prep.isoformat(),
        "next_prep_at": prep.isoformat(),
        "next_event": "Class",
    }
    assert result.wake_at == prep


def test_publish_dry_run_skips_ha(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch("commutecompass.ha_client.call_service") as cs:
        _, ok = publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=_provider({}), dry_run=True)
    assert not ok
    cs.assert_not_called()


def test_publish_requires_ha(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.home_assistant.enabled = False
    with pytest.raises(AlarmConfigError):
        publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=_provider({}))


def test_payload_empty_next_prep(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({}))
    assert build_payload(r)["next_prep_at"] == ""


def test_publish_uses_planned_mapping(tmp_path: Path) -> None:
    """``planned`` short-circuits calendar fetching for that day."""
    cfg = _config(tmp_path)
    prep = _nyc(2026, 10, 16, 9, 0)
    with patch("commutecompass.ha_client.call_service", return_value=True), patch(
        "commutecompass.jobs.tomorrow.plan_window", return_value=[]
    ) as pw:
        result, _ = publish(
            cfg,
            now=_nyc(2026, 10, 15, 13, 0),
            planned={date(2026, 10, 16): [_plan(prep)]},
        )
    assert result.wake_at == prep
    # Today (Oct 15) had no morning run recorded → fetched once; tomorrow came from mapping.
    assert pw.call_count == 1
