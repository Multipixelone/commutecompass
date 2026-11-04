"""Tests for alarm.py — wake computation + publish payload."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from commutecompass.alarm import (
    AlarmConfigError,
    AlarmError,
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
    # Prep-only plan (e.g. a virtual event); commute tests supply leave_at below.
    return Plan(event=ev, prep_at=prep_at)


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


def test_errored_plans_fail_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plans = [_plan(_nyc(2026, 10, 15, 7, 0), error="location_unresolved")]
    with pytest.raises(AlarmError, match="incomplete planning"):
        compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): plans}))


def test_custom_fallback_time(tmp_path: Path) -> None:
    cfg = _config(tmp_path, fallback_time="10:15")
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({}))
    assert r.wake_at == _nyc(2026, 10, 15, 10, 15)


def test_elapsed_early_tomorrow_wake_searches_third_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=180)
    early = _plan(datetime(2026, 10, 16, 2, 30, tzinfo=NYC_TZ))
    seen: list[date] = []

    def provider(day_start: datetime) -> list[Plan]:
        seen.append(day_start.date())
        return [early] if day_start.date() == date(2026, 10, 16) else []

    result = compute_wake(cfg, datetime(2026, 10, 15, 23, 45, tzinfo=NYC_TZ), provider)
    assert seen == [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)]
    assert result.day == date(2026, 10, 17)
    assert result.wake_at == datetime(2026, 10, 17, 12, tzinfo=NYC_TZ)
    assert result.source == "fallback"


def test_extended_wake_horizon_still_rejects_incomplete_planning(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=180)
    early = _plan(datetime(2026, 10, 16, 2, 30, tzinfo=NYC_TZ))
    incomplete = _plan(datetime(2026, 10, 17, 9, tzinfo=NYC_TZ), error="unresolved")

    def provider(day_start: datetime) -> list[Plan]:
        return {date(2026, 10, 16): [early], date(2026, 10, 17): [incomplete]}.get(
            day_start.date(), [],
        )

    with pytest.raises(AlarmError, match="incomplete planning"):
        compute_wake(cfg, datetime(2026, 10, 15, 23, 45, tzinfo=NYC_TZ), provider)


def test_wake_lead_subtracted(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=45)
    prep = _nyc(2026, 10, 15, 9, 0)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [_plan(prep)]}))
    assert r.wake_at == _nyc(2026, 10, 15, 8, 15)
    assert r.next_prep_at == _nyc(2026, 10, 15, 8, 15)


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


def test_tomorrow_planned_even_when_today_wake_future(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=30)
    prep = _nyc(2026, 10, 16, 8)
    prov = _provider({date(2026, 10, 16): [_plan(prep)]})
    result = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), prov)
    assert result.wake_at == _nyc(2026, 10, 15, 12)
    assert result.next_prep_at == _nyc(2026, 10, 16, 7, 30)
    assert prov.calls == [date(2026, 10, 15), date(2026, 10, 16)]


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
        # Prep-only plan without a leave deadline opens no wake window.
        "deadline_at": "",
    }
    assert result.wake_at == prep


def _commute_plan(depart: datetime, *, buffer: int = 13, prep: int = 20) -> Plan:
    leave = (depart.astimezone(UTC) - timedelta(minutes=buffer)).astimezone(NYC_TZ)
    prep_at = (leave.astimezone(UTC) - timedelta(minutes=prep)).astimezone(NYC_TZ)
    plan = _plan(prep_at)
    return plan.model_copy(update={
        "leave_at": leave, "depart_at": depart, "leave_buffer_minutes": buffer,
    })


def test_window_wake_is_leave_deadline_minus_first_lead(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=7)
    depart = _nyc(2026, 10, 15, 9, 0)
    plan = _commute_plan(depart)
    r = compute_wake(
        cfg, _nyc(2026, 10, 15, 5, 0),
        _provider({date(2026, 10, 15): [plan]}),
    )
    assert plan.leave_at is not None
    assert r.wake_at == plan.leave_at - timedelta(minutes=37)
    assert r.deadline_at == plan.leave_at
    # The nap cap follows the same first alarm.
    assert r.next_prep_at == r.wake_at
    payload = build_payload(r, window=True)
    assert payload["deadline_at"] == plan.leave_at.isoformat()
    assert payload["wake_at"] == (plan.leave_at - timedelta(minutes=37)).isoformat()
    assert "departure_at" not in payload


def test_window_uses_leave_without_departure_or_buffer_metadata(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    depart = _nyc(2026, 10, 15, 9, 0)
    plan = _commute_plan(depart).model_copy(update={
        "depart_at": None, "leave_buffer_minutes": None,
    })
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [plan]}))
    assert plan.leave_at is not None
    assert r.deadline_at == plan.leave_at
    assert r.wake_at == plan.leave_at - timedelta(minutes=37)


@pytest.mark.parametrize("buffer_kind,extra_minutes", [
    ("safety", 5), ("weather", 10), ("realtime", 6),
])
def test_buffer_shift_moves_published_window_with_fixed_departure(
    tmp_path: Path, buffer_kind: str, extra_minutes: int,
) -> None:
    cfg = _config(tmp_path)
    depart = _nyc(2026, 10, 15, 9, 0)
    baseline = _commute_plan(depart)
    assert baseline.leave_at is not None
    shifted = baseline.model_copy(update={
        "leave_at": baseline.leave_at - timedelta(minutes=extra_minutes),
        "leave_buffer_minutes": 13 + extra_minutes,
        **({f"{buffer_kind}_buffer_minutes": extra_minutes} if buffer_kind != "safety" else {}),
    })
    assert shifted.depart_at == baseline.depart_at == depart
    # Leave is authoritative even when prep/depart metadata has not changed.
    assert shifted.prep_at == baseline.prep_at
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        for plan in (baseline, shifted):
            publish(
                cfg, now=_nyc(2026, 10, 15, 5, 0),
                plans_for_day=_provider({date(2026, 10, 15): [plan]}),
            )
    before, after = [call.kwargs["data"] for call in cs.call_args_list]
    for field in ("wake_at", "next_prep_at", "deadline_at"):
        assert datetime.fromisoformat(after[field]) == (
            datetime.fromisoformat(before[field]) - timedelta(minutes=extra_minutes)
        )
    assert shifted.leave_at is not None
    assert after["deadline_at"] == shifted.leave_at.isoformat()
    assert "departure_at" not in after


def test_departure_without_leave_deadline_keeps_prep_wake(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 0)).model_copy(update={"leave_at": None})
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [plan]}))
    assert r.wake_at == plan.prep_at
    assert r.deadline_at is None
    assert build_payload(r, window=True)["deadline_at"] == ""


def test_window_disabled_keeps_prep_wake_and_payload(tmp_path: Path) -> None:
    from commutecompass.config import AlarmWindowConfig

    cfg = _config(tmp_path, window=AlarmWindowConfig(enabled=False))
    depart = _nyc(2026, 10, 15, 9, 0)
    plan = _commute_plan(depart)
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [plan]}))
    assert r.wake_at == plan.prep_at
    assert r.deadline_at is None
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        publish(
            cfg, now=_nyc(2026, 10, 15, 5, 0),
            plans_for_day=_provider({date(2026, 10, 15): [plan]}),
        )
    assert "deadline_at" not in cs.call_args.kwargs["data"]


def test_fallback_wake_opens_no_window(tmp_path: Path) -> None:
    """A fallback earlier than leave_at-37m wins and carries no deadline."""
    cfg = _config(tmp_path, fallback_time="07:00")
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 0))
    r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({date(2026, 10, 15): [plan]}))
    assert r.source == "fallback"
    assert r.deadline_at is None
    assert build_payload(r, window=True)["deadline_at"] == ""


def test_window_wake_across_dst_fall_back(tmp_path: Path) -> None:
    """leave_at-37m is absolute: 01:57 EST minus 37m is 01:20 EST, not EDT."""
    cfg = _config(tmp_path)
    depart = datetime(2026, 11, 1, 7, 10, tzinfo=UTC).astimezone(NYC_TZ)  # 02:10 EST
    r = compute_wake(
        cfg, datetime(2026, 11, 1, 5, 0, tzinfo=UTC),  # 01:00 EDT, logical Oct 31
        _provider({date(2026, 11, 1): [_commute_plan(depart)]}),
    )
    assert r.deadline_at == depart - timedelta(minutes=13)
    assert r.wake_at.astimezone(UTC) == datetime(2026, 11, 1, 6, 20, tzinfo=UTC)
    assert r.wake_at.utcoffset() == timedelta(hours=-5)


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
    # Today is still fetched for the nap cap; tomorrow comes from the mapping.
    assert pw.call_count == 1


@pytest.mark.parametrize("hour", [5, 20, 23])
def test_publish_next_future_wake_at_any_hour(tmp_path: Path, hour: int) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=30)
    today = _plan(_nyc(2026, 10, 15, 8))
    tomorrow = _plan(_nyc(2026, 10, 16, 7), "Tomorrow")
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        result, ok = publish(
            cfg, now=_nyc(2026, 10, 15, hour),
            planned={date(2026, 10, 15): [today], date(2026, 10, 16): [tomorrow]},
        )
    assert ok
    expected = _nyc(2026, 10, 15, 7, 30) if hour == 5 else _nyc(2026, 10, 16, 6, 30)
    assert result.wake_at == expected
    assert cs.call_args.kwargs["data"]["wake_at"] == expected.isoformat()


@pytest.mark.parametrize("snoozed", [False, True])
@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("future_today", [False, True])
def test_published_upcoming_schedule_preserves_active_wake(
    tmp_path: Path, snoozed: bool, window: bool, future_today: bool,
) -> None:
    """Mock the publisher's HA write, then parse it through the real daemon reader."""
    from dataclasses import replace

    from commutecompass.alarm_daemon import DaemonState, Settings, inputs_from_states, step

    cfg = _config(tmp_path, window={"enabled": window}, fallback_time="23:00")
    now = _nyc(2026, 10, 15, 17, 3)
    original_wake = now - timedelta(minutes=1)
    original_deadline = original_wake + timedelta(minutes=37) if window else None
    entities: dict[str, Any] = {
        cfg.alarm.wake_utc_entity: {"state": original_wake.isoformat()},
        cfg.alarm.window.deadline_utc_entity: {
            "state": original_deadline.isoformat() if original_deadline else "unknown",
        },
    }
    settings = Settings.from_config(cfg.alarm)
    inputs = inputs_from_states(entities, cfg.alarm)
    active, _ = step(DaemonState(), original_wake, inputs, settings)
    if snoozed:
        active, _ = step(active, original_wake + timedelta(seconds=30), replace(
            inputs, snooze_pressed_at=original_wake + timedelta(seconds=15),
        ), settings)
    assert active.mode == ("snoozed" if snoozed else "ringing")
    historical = _commute_plan(_nyc(2026, 10, 15, 17, 10))
    historical.error = "too_imminent"
    later = _commute_plan(_nyc(2026, 10, 15, 18))
    # A removed morning event may leave a later upcoming plan; a retained
    # historical too-imminent plan must still prevent a second wake today.
    today = [later] if future_today else [historical]
    tomorrow_prep = _nyc(2026, 10, 16, 8)
    upcoming = _plan(tomorrow_prep, "Tomorrow")

    def write_helpers(*_args: Any, **kwargs: Any) -> bool:
        data = kwargs["data"]
        entities[cfg.alarm.wake_utc_entity] = {"state": data["wake_at"]}
        entities[cfg.alarm.window.deadline_utc_entity] = {
            "state": data.get("deadline_at") or "unknown",
        }
        return True

    with patch("commutecompass.ha_client.call_service", side_effect=write_helpers) as service:
        result, accepted = publish(cfg, now=now, plans_for_day=_provider({
            now.date(): today, tomorrow_prep.date(): [upcoming],
        }))
    assert accepted and service.call_count == 1
    expected = tomorrow_prep
    if future_today:
        expected = _nyc(2026, 10, 15, 17, 10) if window else _nyc(2026, 10, 15, 17, 27)
    assert result.wake_at == expected and result.plan is not historical
    assert entities[cfg.alarm.wake_utc_entity]["state"] == result.wake_at.isoformat()
    state, actions = step(active, now, inputs_from_states(entities, cfg.alarm), settings)
    assert actions == [] and state.mode == active.mode and state.active == original_wake
    assert state.session is not None and active.session is not None
    assert state.session.id == active.session.id
    assert state.session.original_at == original_wake
    assert state.session.locked_deadline_at == original_deadline
    assert state.ring is not None and active.ring is not None
    assert state.ring.id == active.ring.id and state.ring.started_at == active.ring.started_at
    assert state.snooze_until == active.snooze_until
    assert state.deadline == original_deadline and state.ended_wake is None


@pytest.mark.parametrize("bad_day", [15, 16])
def test_partial_planning_never_posts_fallback(tmp_path: Path, bad_day: int) -> None:
    cfg = _config(tmp_path)
    plans = [_plan(_nyc(2026, 10, bad_day, 21), error="no_route")]
    with patch("commutecompass.ha_client.call_service") as cs, pytest.raises(
        AlarmError, match="incomplete planning"
    ):
        publish(
            cfg, now=_nyc(2026, 10, 15, 20),
            plans_for_day=_provider({date(2026, 10, bad_day): plans}),
        )
    cs.assert_not_called()


def test_calendar_failure_is_not_empty_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch("commutecompass.jobs.tomorrow.plan_window", return_value=None), patch(
        "commutecompass.ha_client.call_service"
    ) as cs, pytest.raises(AlarmError, match="calendar fetch failed"):
        publish(cfg, now=_nyc(2026, 10, 15, 20))
    cs.assert_not_called()


def test_elapsed_tomorrow_wake_publishes_following_day(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=180)
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        result = publish(
            cfg, now=_nyc(2026, 10, 16, 1, 30),
            plans_for_day=_provider({date(2026, 10, 16): [_plan(_nyc(2026, 10, 16, 3))]}),
        )
    assert result[0].wake_at == _nyc(2026, 10, 17, 12)
    cs.assert_called_once()


def test_future_prep_cap_can_be_already_due(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=45)
    result = compute_wake(
        cfg, _nyc(2026, 10, 15, 8, 30),
        _provider({date(2026, 10, 15): [_plan(_nyc(2026, 10, 15, 9))]}),
    )
    assert result.wake_at == _nyc(2026, 10, 16, 12)
    assert result.next_prep_at == _nyc(2026, 10, 15, 8, 15)


@pytest.mark.parametrize("ok", [True, False])
def test_publish_heartbeat_only_on_success(tmp_path: Path, ok: bool) -> None:
    from commutecompass.store import Store

    cfg = _config(tmp_path)
    store = Store(cfg.paths.db_path)
    store.init_schema()
    now = _nyc(2026, 10, 15, 20)
    with patch("commutecompass.ha_client.call_service", return_value=ok):
        publish(cfg, now=now, store=store, plans_for_day=_provider({}))
    assert store.get_job_heartbeat("alarm_publish") == (now if ok else None)


@pytest.mark.parametrize("elapsed_minutes", [None, 14, 15])
def test_poll_publishes_periodically(tmp_path: Path, elapsed_minutes: int | None) -> None:
    from unittest.mock import Mock

    from commutecompass.jobs.poll import run
    from commutecompass.store import Store

    cfg = _config(tmp_path)
    cfg.home_assistant.enabled = False
    now = _nyc(2026, 10, 15, 20)
    store = Store(cfg.paths.db_path)
    store.init_schema()
    if elapsed_minutes is not None:
        store.record_job_success("alarm_publish", now - timedelta(minutes=elapsed_minutes))
    with patch("commutecompass.alarm.publish_safe") as pub:
        run(
            cfg, store=store, notifier=Mock(), plan_event_fn=Mock(),
            fetch_alerts_fn=lambda **_: [], now_fn=lambda: now,
        )
    if elapsed_minutes == 14:
        pub.assert_not_called()
    else:
        pub.assert_called_once_with(cfg, store=store, now=now)


@pytest.mark.parametrize("hour", [8, 20])
def test_historical_too_imminent_keeps_first_wake_elapsed(tmp_path: Path, hour: int) -> None:
    cfg = _config(tmp_path)
    historical = _plan(_nyc(2026, 10, 15, 7), error="too_imminent")
    later = _plan(_nyc(2026, 10, 15, 10), "Later today")
    tomorrow = _plan(_nyc(2026, 10, 16, 8), "Tomorrow")
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs:
        result, ok = publish(
            cfg, now=_nyc(2026, 10, 15, hour),
            plans_for_day=_provider({
                date(2026, 10, 15): [historical, later],
                date(2026, 10, 16): [tomorrow],
            }),
        )
    assert ok
    assert result.wake_at == tomorrow.prep_at
    assert result.next_prep_at == (later.prep_at if hour == 8 else tomorrow.prep_at)
    cs.assert_called_once()


def test_historical_too_imminent_does_not_hide_afternoon_cap(tmp_path: Path) -> None:
    cfg = _config(tmp_path, wake_lead_minutes=30)
    result = compute_wake(
        cfg, _nyc(2026, 10, 15, 13), _provider({
            date(2026, 10, 15): [
                _plan(_nyc(2026, 10, 15, 7), error="too_imminent"),
                _plan(_nyc(2026, 10, 15, 16), "Afternoon"),
            ],
            date(2026, 10, 16): [_plan(_nyc(2026, 10, 16, 8))],
        }),
    )
    assert result.wake_at == _nyc(2026, 10, 16, 7, 30)
    assert result.next_prep_at == _nyc(2026, 10, 15, 15, 30)
    assert result.next_event == "Afternoon"


@pytest.mark.parametrize("historical", [False, True])
def test_started_window_rolls_to_next_morning_not_evening(
    tmp_path: Path, historical: bool,
) -> None:
    from commutecompass.config import AlarmWindowConfig

    cfg = _config(tmp_path, window=AlarmWindowConfig(first_alarm_lead_minutes=60))
    prep, leave = (_nyc(2026, 11, 4, 7, 10), _nyc(2026, 11, 4, 7, 40)) if historical else (
        datetime(2026, 11, 4, 8, 15, 47, tzinfo=NYC_TZ),
        datetime(2026, 11, 4, 8, 45, 47, tzinfo=NYC_TZ))
    morning = _plan(_nyc(2026, 11, 4, 9), "Morning").model_copy(update={
        "prep_at": prep, "leave_at": leave, "error": "too_imminent" if historical else None})
    evening = _plan(datetime(2026, 11, 4, 18, 30, tzinfo=NYC_TZ), "Evening").model_copy(update={
        "prep_at": datetime(2026, 11, 4, 17, 33, 57, tzinfo=NYC_TZ),
        "leave_at": datetime(2026, 11, 4, 18, 3, 57, tzinfo=NYC_TZ)})
    tomorrow = _plan(_nyc(2026, 11, 5, 8)).model_copy(update={"leave_at": _nyc(2026, 11, 5, 8, 30)})
    r = compute_wake(cfg, _nyc(2026, 11, 4, 8), _provider({
        date(2026, 11, 4): [morning, evening], date(2026, 11, 5): [tomorrow],
    }))
    assert (r.day, r.wake_at, r.plan) == (date(2026, 11, 5), _nyc(2026, 11, 5, 7, 30), tomorrow)
    if not historical:  # Nap cap stays on the started window, not the evening.
        assert r.next_prep_at == datetime(2026, 11, 4, 7, 45, 47, tzinfo=NYC_TZ)


@pytest.mark.parametrize("error", ["no_route", "location_unresolved", "internal_error"])
def test_past_unresolved_start_bounds_elapsed_wake(tmp_path: Path, error: str) -> None:
    cfg = _config(tmp_path)
    unknown = _plan(_nyc(2026, 10, 15, 6), error=error)
    unknown.prep_at = None
    unknown.leave_at = None
    result = compute_wake(
        cfg, _nyc(2026, 10, 15, 8), _provider({
            date(2026, 10, 15): [unknown, _plan(_nyc(2026, 10, 15, 10), "Later")],
            date(2026, 10, 16): [_plan(_nyc(2026, 10, 16, 9))],
        }),
    )
    assert result.wake_at == _nyc(2026, 10, 16, 9)
    assert result.next_prep_at == _nyc(2026, 10, 15, 10)


@pytest.mark.parametrize("error", ["no_route", "location_unresolved", "too_imminent"])
def test_historical_success_does_not_excuse_future_error(tmp_path: Path, error: str) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(AlarmError, match="incomplete planning"):
        compute_wake(
            cfg, _nyc(2026, 10, 15, 13), _provider({
                date(2026, 10, 15): [
                    _plan(_nyc(2026, 10, 15, 7), error="too_imminent"),
                    _plan(_nyc(2026, 10, 15, 16), error=error),
                ],
            }),
        )


# ─────────── schedule contract v1 ───────────

class _FakeHA:
    """Mock HA: script call result + scripted readbacks of the schedule helper."""

    def __init__(self, *, call_ok: bool = True, readbacks: list[str | None] | None = None,
                 commit: bool = True) -> None:
        self.call_ok = call_ok
        self.readbacks = list(readbacks or [])
        self.commit = commit
        self.helper: str | None = "old"
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.reads: list[str] = []

    def call_service(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((args, kwargs))
        raw = kwargs["data"]["snapshot"]
        # Like HA's input_text (max: 255) / script length guard: the *raw
        # received* string is what must fit, so check it here, not upstream.
        assert isinstance(raw, str) and len(raw) <= 255, len(raw)
        if self.commit:
            self.helper = kwargs["data"]["snapshot"]
        return self.call_ok

    def fetch(self, base_url: str, token: str, entity_id: str, **_: Any) -> str | None:
        self.reads.append(entity_id)
        if self.readbacks:
            return self.readbacks.pop(0)
        return self.helper


def _publish_v1(tmp_path: Path, ha: _FakeHA, plans: list[Plan], **alarm: Any) -> Any:
    cfg = _config(tmp_path, schedule_contract="v1", **alarm)
    with patch("commutecompass.ha_client.call_service", side_effect=ha.call_service), patch(
        "commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch,
    ):
        return publish(
            cfg, now=_nyc(2026, 10, 15, 5, 0, ),
            plans_for_day=_provider({date(2026, 10, 15): plans}),
        )


def test_v1_publishes_canonical_snapshot_and_confirms(tmp_path: Path) -> None:
    from commutecompass.alarm_contract import parse_schedule

    ha = _FakeHA()
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 0))
    result, ok = _publish_v1(tmp_path, ha, [plan])
    assert ok
    assert len(ha.calls) == 1
    args, kwargs = ha.calls[0]
    assert args[:4] == ("http://ha.local:8123", "ha-tok", "script", "commute_publish_alarm_v1")
    assert set(kwargs["data"]) == {"snapshot", "next_event"}
    assert kwargs["data"]["next_event"] == "Class"
    snap = parse_schedule(kwargs["data"]["snapshot"])
    assert snap.kind == "commute"
    assert snap.wake_at == result.wake_at
    assert snap.deadline_at == result.deadline_at == plan.leave_at
    assert snap.next_prep_at == result.next_prep_at
    assert snap.issued_at == _nyc(2026, 10, 15, 5, 0)
    assert '"issued_at":"2026-10-15T09:00:00Z"' in kwargs["data"]["snapshot"]
    assert ha.reads == ["input_text.commute_alarm_schedule_v1"]


def test_v1_prep_only_wake_is_legacy_kind(tmp_path: Path) -> None:
    from commutecompass.alarm_contract import parse_schedule

    ha = _FakeHA()
    _publish_v1(tmp_path, ha, [_plan(_nyc(2026, 10, 15, 8, 0))])
    snap = parse_schedule(ha.calls[0][1]["data"]["snapshot"])
    assert snap.kind == "legacy" and snap.deadline_at is None


def test_v1_fallback_wake_is_legacy_kind(tmp_path: Path) -> None:
    from commutecompass.alarm_contract import parse_schedule

    ha = _FakeHA()
    _publish_v1(tmp_path, ha, [])
    snap = parse_schedule(ha.calls[0][1]["data"]["snapshot"])
    assert snap.kind == "legacy"
    assert snap.wake_at == _nyc(2026, 10, 15, 12, 0)


def test_v1_custom_first_lead_round_trips(tmp_path: Path) -> None:
    from commutecompass.alarm_contract import parse_schedule
    from commutecompass.config import AlarmWindowConfig

    ha = _FakeHA()
    _publish_v1(tmp_path, ha, [_commute_plan(_nyc(2026, 10, 15, 9, 0))],
                window=AlarmWindowConfig(first_alarm_lead_minutes=30))
    raw = ha.calls[0][1]["data"]["snapshot"]
    assert parse_schedule(raw, first_lead_seconds=1800).kind == "commute"


@pytest.mark.parametrize("readbacks", [
    [None, None],  # helper unreadable
    ["old", "old"],  # HA refused (e.g. stale/conflict) — helper unchanged
    ["garbage", "garbage"],
])
def test_v1_call_success_without_exact_readback_raises(
    tmp_path: Path, readbacks: list[str | None],
) -> None:
    ha = _FakeHA(readbacks=readbacks)
    with pytest.raises(AlarmError):
        _publish_v1(tmp_path, ha, [_commute_plan(_nyc(2026, 10, 15, 9, 0))])
    # Never falls back to the legacy publish script.
    assert all(c[0][3] == "commute_publish_alarm_v1" for c in ha.calls)


def test_v1_retry_reuses_identity_and_body(tmp_path: Path) -> None:
    ha = _FakeHA(call_ok=False, commit=False)
    with pytest.raises(AlarmError):
        _publish_v1(tmp_path, ha, [_commute_plan(_nyc(2026, 10, 15, 9, 0))])
    assert len(ha.calls) == 2
    assert ha.calls[0][1]["data"] == ha.calls[1][1]["data"]
    assert all(c[0][3] == "commute_publish_alarm_v1" for c in ha.calls)


def test_v1_lost_response_with_visible_commit_succeeds(tmp_path: Path) -> None:
    ha = _FakeHA(call_ok=False, commit=True)
    _, ok = _publish_v1(tmp_path, ha, [_commute_plan(_nyc(2026, 10, 15, 9, 0))])
    assert ok and len(ha.calls) == 1


def test_v1_second_attempt_confirms(tmp_path: Path) -> None:
    ha = _FakeHA(readbacks=["old"])
    _, ok = _publish_v1(tmp_path, ha, [_commute_plan(_nyc(2026, 10, 15, 9, 0))])
    assert ok and len(ha.calls) == 2
    assert ha.calls[0][1]["data"] == ha.calls[1][1]["data"]


def test_v1_failure_via_publish_safe_never_writes_legacy(tmp_path: Path) -> None:
    from commutecompass.alarm import publish_safe

    ha = _FakeHA(call_ok=False, commit=False)
    cfg = _config(tmp_path, schedule_contract="v1")
    with patch("commutecompass.ha_client.call_service", side_effect=ha.call_service), patch(
        "commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch,
    ), patch("commutecompass.alarm.compute_wake") as cw:
        cw.return_value = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0), _provider({}))
        publish_safe(cfg, now=_nyc(2026, 10, 15, 5, 0), store=None, planned={})
    assert ha.calls and all(c[0][3] == "commute_publish_alarm_v1" for c in ha.calls)


def test_v1_dry_run_skips_ha(tmp_path: Path) -> None:
    ha = _FakeHA()
    cfg = _config(tmp_path, schedule_contract="v1")
    with patch("commutecompass.ha_client.call_service", side_effect=ha.call_service):
        _, ok = publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=_provider({}),
                        dry_run=True)
    assert not ok and not ha.calls


def test_v1_malformed_script_id_rejected_before_planning(tmp_path: Path) -> None:
    cfg = _config(tmp_path, schedule_contract="v1", publish_v1_script="nodot")
    provider = _provider({})
    with pytest.raises(AlarmConfigError):
        publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=provider)
    assert provider.calls == []


def test_fetch_entity_state_parses_and_fails_closed() -> None:
    import httpx

    from commutecompass.alarm import fetch_entity_state

    def run(resp: Any) -> str | None:
        with patch("httpx.Client") as client:
            get = client.return_value.__enter__.return_value.get
            if isinstance(resp, Exception):
                get.side_effect = resp
            else:
                get.return_value = resp
            return fetch_entity_state("http://ha/", "tok", "input_text.x")

    assert run(httpx.Response(200, json={"state": "abc"})) == "abc"
    assert run(httpx.Response(200, json={"state": 1})) is None
    assert run(httpx.Response(200, json=[])) is None
    assert run(httpx.Response(200, text="nope")) is None
    assert run(httpx.Response(404, json={"state": "abc"})) is None
    assert run(httpx.ConnectError("x")) is None
    with patch("httpx.Client") as client:
        get = client.return_value.__enter__.return_value.get
        get.return_value = httpx.Response(200, json={"state": "s"})
        fetch_entity_state("http://ha/", "tok", "input_text.x")
        assert get.call_args.args[0] == "http://ha/api/states/input_text.x"


def test_legacy_default_publish_unchanged(tmp_path: Path) -> None:
    """Default contract keeps the legacy script and never reads the v1 helper."""
    cfg = _config(tmp_path)
    assert cfg.alarm.schedule_contract == "legacy"
    with patch("commutecompass.ha_client.call_service", return_value=True) as cs, patch(
        "commutecompass.alarm.fetch_entity_state",
    ) as fetch:
        publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=_provider({}))
    fetch.assert_not_called()
    assert cs.call_args.args[3] == "commute_publish_alarm"
    assert set(cs.call_args.kwargs["data"]) == {"wake_at", "next_prep_at", "next_event", "deadline_at"}


def test_v1_planner_failure_publishes_nothing(tmp_path: Path) -> None:
    """Incomplete planning raises; it is never turned into a kind='none' clear."""
    ha = _FakeHA()
    cfg = _config(tmp_path, schedule_contract="v1")

    def broken(day_start: datetime) -> list[Plan]:
        raise AlarmError("calendar fetch failed")

    with patch("commutecompass.ha_client.call_service", side_effect=ha.call_service), patch(
        "commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch,
    ):
        with pytest.raises(AlarmError):
            publish(cfg, now=_nyc(2026, 10, 15, 5, 0), plans_for_day=broken)
        # Background path swallows the error but still sends nothing.
        from commutecompass.alarm import publish_safe

        with patch("commutecompass.alarm._default_provider", return_value=broken):
            publish_safe(cfg, now=_nyc(2026, 10, 15, 5, 0), store=None, planned={})
    assert ha.calls == [] and ha.reads == []
    assert ha.helper == "old"


def test_v1_errored_plan_publishes_nothing(tmp_path: Path) -> None:
    ha = _FakeHA()
    with pytest.raises(AlarmError):
        _publish_v1(tmp_path, ha, [_plan(_nyc(2026, 10, 15, 8, 0), error="no route")])
    assert ha.calls == []


def test_v1_publisher_never_emits_none_kind(tmp_path: Path) -> None:
    """No events → fallback ``legacy`` wake; ``none`` is only built deliberately."""
    from commutecompass.alarm import build_schedule
    from commutecompass.alarm_contract import new_schedule, parse_schedule

    cfg = _config(tmp_path)
    for plans in ([], [_plan(_nyc(2026, 10, 15, 8, 0))],
                  [_commute_plan(_nyc(2026, 10, 15, 9, 0))]):
        r = compute_wake(cfg, _nyc(2026, 10, 15, 5, 0),
                         _provider({date(2026, 10, 15): plans}))
        assert build_schedule(r, cfg, issued_at=_nyc(2026, 10, 15, 5, 0)).kind != "none"
    # The manual/test helper can still build a deliberate clear.
    clear = new_schedule(issued_at=_nyc(2026, 10, 15, 5, 0), kind="none",
                         wake_at=None, deadline_at=None, next_prep_at=None)
    assert parse_schedule(build_payload_v1_snapshot(clear)).kind == "none"


def build_payload_v1_snapshot(schedule: Any) -> str:
    from commutecompass.alarm_contract import serialize_schedule

    return serialize_schedule(schedule)


def test_v1_next_prep_is_effective_first_alarm(tmp_path: Path) -> None:
    """``next_prep_at`` carries the window start (leave - lead), not raw prep_at."""
    from commutecompass.alarm_contract import parse_schedule

    ha = _FakeHA()
    plan = _commute_plan(_nyc(2026, 10, 15, 9, 0))
    assert plan.leave_at is not None and plan.prep_at is not None
    _publish_v1(tmp_path, ha, [plan])
    snap = parse_schedule(ha.calls[0][1]["data"]["snapshot"])
    first = plan.leave_at - timedelta(minutes=37)
    assert snap.next_prep_at == first == snap.wake_at
    assert snap.next_prep_at != plan.prep_at


_US = timedelta(microseconds=123456)


@pytest.mark.parametrize(("kind", "prep", "micro", "length"), [
    ("commute", False, False, 189), ("commute", False, True, 210),
    ("commute", True, False, 207), ("commute", True, True, 235),
    ("legacy", False, False, 170), ("legacy", False, True, 184),
    ("legacy", True, False, 188), ("legacy", True, True, 209),
    ("none", False, False, 150), ("none", False, True, 157),
])
def test_v1_received_snapshot_length_per_kind(
    tmp_path: Path, kind: str, prep: bool, micro: bool, length: int,
) -> None:
    """Length of the raw string the mocked HA script receives, every variant."""
    from commutecompass.alarm import publish_v1
    from commutecompass.alarm_contract import new_schedule, parse_schedule

    m = _US if micro else timedelta(0)
    deadline = datetime(2026, 11, 4, 13, 0, tzinfo=UTC) + m
    sched = new_schedule(
        issued_at=datetime(2026, 11, 4, 1, 0, tzinfo=UTC) + m,
        kind=kind,  # type: ignore[arg-type]
        wake_at=None if kind == "none" else deadline - timedelta(minutes=37),
        deadline_at=deadline if kind == "commute" else None,
        next_prep_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC) + m if prep else None,
        schedule_id="6b6e9c724ace4c81a350ef144a7ef39b",
    )
    ha = _FakeHA()
    cfg = _config(tmp_path, schedule_contract="v1")
    with patch("commutecompass.ha_client.call_service", side_effect=ha.call_service), patch(
        "commutecompass.alarm.fetch_entity_state", side_effect=ha.fetch,
    ):
        returned = publish_v1(cfg, sched, "Example event")
    received = ha.calls[0][1]["data"]["snapshot"]
    assert isinstance(received, str)
    assert len(received) == length <= 255
    assert received == returned == ha.helper
    assert ("." in received) is micro
    assert parse_schedule(received) == sched
