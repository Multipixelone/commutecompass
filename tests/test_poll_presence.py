"""Ephemeral HA destination presence: atomic deferral, not persisted arrival state."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from commutecompass.config import Config
from commutecompass.jobs.poll import _present_at_destination, run
from commutecompass.models import CurrentLocation, Event, PingEntry, Plan, ResolvedLocation, Route
from commutecompass.store import Store
from commutecompass.timeutil import NYC_TZ

NOW = datetime(2026, 11, 1, 12, tzinfo=NYC_TZ)


@pytest.fixture
def config(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr("commutecompass.timeutil.now_nyc", lambda: NOW)
    return Config.model_validate({
        "origin": {"address": "Synthetic origin", "lat": 0, "lon": 0},
        "calendars": [],
        "prep": {}, "scheduling": {},
        "paths": {"db_path": str(tmp_path / "presence.db"),
                  "venues_file": str(tmp_path / "venues.yaml"),
                  "oauth_token_path": str(tmp_path / "oauth.json")},
        "opencode_go": {"endpoint": "http://example.invalid"},
        "mta": {"subway_alerts_url": "", "lirr_alerts_url": "", "bus_alerts_url": ""},
        "home_assistant": {"enabled": True},
    })


@pytest.fixture
def store(config: Config) -> Store:
    result = Store(config.paths.db_path)
    result.init_schema()
    return result


def plan_at(event_id: str = "second-shift", *, lon: float = 0) -> Plan:
    return Plan(
        event=Event(
            id=event_id, calendar_id="c", calendar_name="Calendar", title="Shift",
            start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2),
            location_resolved=ResolvedLocation(
                kind="address", value="Synthetic venue", lat=0, lon=lon, source="geocode",
            ),
        ),
        route=Route(legs=[], depart_at=NOW + timedelta(minutes=10),
                    arrive_at=NOW + timedelta(minutes=40), total_duration_seconds=1800),
        prep_at=NOW - timedelta(minutes=1), leave_at=NOW + timedelta(minutes=10),
    )


def fix(**updates: Any) -> CurrentLocation:
    return CurrentLocation(lat=0, lon=0, accuracy_m=10, captured_at=NOW).model_copy(update=updates)


def poll(config: Config, store: Store, **overrides: Any) -> tuple[MagicMock, MagicMock]:
    notifier, alarm = MagicMock(), MagicMock()
    notifier.send.return_value = True
    options: dict[str, Any] = {
        "store": store, "notifier": notifier, "ha_alarm_notifier": alarm,
        "now_fn": lambda: NOW, "ha_fetch_fn": lambda *a, **kw: None,
        "ha_zones_fn": lambda *a, **kw: {}, "fetch_alerts_fn": lambda **kw: [],
        "select_alerts_fn": lambda *a, **kw: [], "plan_event_fn": MagicMock(),
    }
    options.update(overrides)
    run(config, **options)
    return notifier, alarm


def schedule(store: Store, plan: Plan, *, retry: int = 0) -> None:
    store.upsert_plan(plan)
    for kind in ("prep", "leave"):
        store.schedule_ping(PingEntry(
            id=f"{plan.event.id}-{kind}", event_id=plan.event.id, kind=kind,
            fire_at=NOW - timedelta(minutes=1), message=kind,
        ))
        with store._connect() as conn:
            conn.execute("UPDATE pings SET send_attempts = ? WHERE id = ?",
                         (retry, f"{plan.event.id}-{kind}"))


@pytest.mark.parametrize("changes", [
    {"accuracy_m": None}, {"accuracy_m": -1}, {"accuracy_m": float("nan")},
    {"accuracy_m": float("inf")}, {"accuracy_m": 101},
    {"lat": 91}, {"lon": -181}, {"lat": float("nan")}, {"lon": float("inf")},
    {"captured_at": NOW - timedelta(minutes=5, microseconds=1)},
    {"captured_at": NOW + timedelta(microseconds=1)},
    {"captured_at": NOW.replace(tzinfo=None)},
])
def test_unreliable_fix_fails_open(config: Config, changes: dict[str, Any]) -> None:
    assert not _present_at_destination(config, plan_at(), fix(**changes), NOW)


@pytest.mark.parametrize("case", ["disabled", "missing_fix", "missing_plan", "no_destination",
                                 "missing_coords", "invalid_coords", "nonfinite_coords"])
def test_missing_evidence_fails_open(config: Config, case: str) -> None:
    plan: Plan | None = plan_at()
    location: CurrentLocation | None = fix()
    if case == "disabled":
        config.home_assistant.enabled = False
    elif case == "missing_fix":
        location = None
    elif case == "missing_plan":
        plan = None
    elif case == "no_destination":
        assert plan is not None
        plan.event.location_resolved = None
    else:
        assert plan is not None
        assert plan.event.location_resolved is not None
        plan.event.location_resolved.lat = {
            "missing_coords": None, "invalid_coords": -91, "nonfinite_coords": float("nan"),
        }[case]
    assert not _present_at_destination(config, plan, location, NOW)


def test_radius_accuracy_and_age_boundaries(config: Config) -> None:
    plan = plan_at()
    assert _present_at_destination(config, plan, fix(accuracy_m=100), NOW)
    assert not _present_at_destination(config, plan, fix(accuracy_m=100.001), NOW)
    offset = math.degrees(60 / 6_371_000)
    assert _present_at_destination(config, plan, fix(lat=offset, accuracy_m=39.999), NOW)
    assert not _present_at_destination(config, plan, fix(lat=offset, accuracy_m=40.001), NOW)
    config.home_assistant.min_gps_accuracy_meters = 5
    assert not _present_at_destination(config, plan, fix(), NOW)
    config.home_assistant.min_gps_accuracy_meters = 0
    assert _present_at_destination(config, plan, fix(), NOW)
    assert _present_at_destination(config, plan, fix(captured_at=NOW - timedelta(minutes=5)), NOW)
    config.home_assistant.max_age_minutes = 2
    assert _present_at_destination(config, plan, fix(captured_at=NOW - timedelta(minutes=2)), NOW)
    assert not _present_at_destination(
        config, plan, fix(captured_at=NOW - timedelta(minutes=2, microseconds=1)), NOW,
    )


def test_presence_uses_utc_instants_across_nyc_fold(config: Config) -> None:
    now = datetime(2026, 11, 1, 1, 2, tzinfo=NYC_TZ, fold=1)
    captured = datetime(2026, 11, 1, 1, 59, tzinfo=NYC_TZ, fold=0)
    assert _present_at_destination(config, plan_at(), fix(captured_at=captured), now)
    assert not _present_at_destination(config, plan_at(), fix(captured_at=now), captured)
    assert not _present_at_destination(
        config, plan_at(), fix(captured_at=now.replace(fold=0)), now,
    )


def test_same_venue_shifts_defer_without_mutating_then_depart_once(config: Config, store: Store) -> None:
    first, second = plan_at("first-shift"), plan_at()
    first.event.start = NOW - timedelta(hours=2)
    first.event.end = NOW - timedelta(hours=1)
    store.upsert_plan(first)
    schedule(store, second, retry=2)
    store.upsert_current_location(fix())
    before = store.pending_pings(NOW)
    planner = MagicMock()
    for _ in range(2):
        notifier, alarm = poll(config, store, plan_event_fn=planner)
        notifier.send.assert_not_called()
        alarm.send.assert_not_called()
        assert store.pending_pings(NOW) == before
        assert store.get_plan(second.event.id) == second
    planner.assert_not_called()
    # Depart: deferred rows are still due. Leave the replan window to isolate dispatch.
    store.upsert_current_location(fix(lon=1))
    config.home_assistant.replan_window_minutes = 0
    notifier, alarm = poll(config, store)
    assert notifier.send.call_count == 2
    assert alarm.send.call_count == 2
    assert store.pending_pings(NOW) == []
    notifier, alarm = poll(config, store)
    notifier.send.assert_not_called()
    alarm.send.assert_not_called()


def test_presence_does_not_suppress_other_venue_digest_or_service(config: Config, store: Store) -> None:
    here, elsewhere = plan_at(), plan_at("other", lon=1)
    schedule(store, here)
    schedule(store, elsewhere)
    config.home_assistant.replan_window_minutes = 0
    store.upsert_current_location(fix())
    for kind in ("digest", "service_update"):
        store.schedule_ping(PingEntry(id=kind, event_id=here.event.id, kind=kind,
                                      fire_at=NOW, message=kind))
    notifier, alarm = poll(config, store)
    assert sorted(call.args[0] for call in notifier.send.call_args_list) == [
        "digest", "leave", "prep", "service_update",
    ]
    assert alarm.send.call_count == 2
    assert len(store.pending_pings(NOW)) == 2


@pytest.mark.parametrize("enabled,location", [
    (False, fix()), (True, None), (True, fix(accuracy_m=None)),
    (True, fix(accuracy_m=150)), (True, fix(lat=100)),
    (True, fix(captured_at=NOW - timedelta(minutes=6))),
    (True, fix(captured_at=NOW + timedelta(seconds=1))),
    (True, fix(captured_at=NOW.replace(tzinfo=None))),
])
def test_due_dispatch_fails_open(config: Config, store: Store, enabled: bool,
                                location: CurrentLocation | None) -> None:
    config.home_assistant.enabled = enabled
    config.home_assistant.replan_window_minutes = 0
    schedule(store, plan_at())
    if location is not None:
        store.upsert_current_location(location)
    notifier, alarm = poll(config, store)
    assert notifier.send.call_count == 2
    assert alarm.send.call_count == 2


@pytest.mark.parametrize("present", [True, False])
def test_event_start_consumes_stale_alarms_without_sending(config: Config, store: Store,
                                                         present: bool) -> None:
    plan = plan_at()
    plan.event.start = NOW
    schedule(store, plan, retry=3)
    if present:
        store.upsert_current_location(fix())
    notifier, alarm = poll(config, store)
    notifier.send.assert_not_called()
    alarm.send.assert_not_called()
    assert store.pending_pings(NOW) == []
    with store._connect() as conn:
        rows = conn.execute("SELECT fired, fired_at, send_attempts FROM pings").fetchall()
    assert all(tuple(row) == (1, NOW.isoformat(), 3) for row in rows)


def test_no_initial_send_grace_cutoff(config: Config, store: Store) -> None:
    config.home_assistant.enabled = False
    schedule(store, plan_at())
    with store._connect() as conn:
        conn.execute("UPDATE pings SET fire_at = ?", ((NOW - timedelta(hours=1)).isoformat(),))
    notifier, _ = poll(config, store)
    assert notifier.send.call_count == 2


def test_muted_present_rows_still_consumed(config: Config, store: Store) -> None:
    schedule(store, plan_at())
    store.mute_event("second-shift")
    store.upsert_current_location(fix())
    notifier, alarm = poll(config, store)
    notifier.send.assert_not_called()
    alarm.send.assert_not_called()
    assert store.pending_pings(NOW) == []


@pytest.mark.parametrize("change", ["plan", "location", "payload", "expired_plan"])
def test_atomic_claim_uses_current_plan_location_and_payload(config: Config, store: Store,
                                                            change: str) -> None:
    plan = plan_at(lon=1 if change == "plan" else 0)
    schedule(store, plan)
    store.upsert_current_location(fix(lon=1))
    config.home_assistant.replan_window_minutes = 0
    original = store.pending_pings

    def enumerate_then_change(before: datetime) -> list[PingEntry]:
        snapshot = original(before)
        if change == "plan":
            store.upsert_current_location(fix())
            assert plan.event.location_resolved is not None
            plan.event.location_resolved.lon = 0
            store.upsert_plan(plan)
        elif change == "location":
            Store(store.db_path).upsert_current_location(fix())
        elif change == "expired_plan":
            plan.event.start = NOW
            store.upsert_plan(plan)
        else:
            store.upsert_current_location(fix())
            with store._connect() as conn:
                conn.execute("UPDATE pings SET kind = CASE kind WHEN 'prep' THEN 'digest' "
                             "ELSE 'service_update' END, message = 'current'")
        return snapshot

    with patch.object(store, "pending_pings", side_effect=enumerate_then_change):
        notifier, alarm = poll(config, store)
    if change == "payload":
        assert [c.args[0] for c in notifier.send.call_args_list] == ["current", "current"]
        alarm.send.assert_not_called()
    else:
        notifier.send.assert_not_called()
        alarm.send.assert_not_called()
    assert len(original(NOW)) == (2 if change in {"plan", "location"} else 0)


@pytest.mark.parametrize("during", ["arrive", "stale", "future_becomes_current", "cas"])
def test_replan_rechecks_live_presence_after_blocking(config: Config, store: Store,
                                                     during: str) -> None:
    plan = plan_at()
    store.upsert_plan(plan)
    current = NOW
    # Start away, so planning is allowed. Fix changes during the blocking call.
    store.upsert_current_location(fix(lon=1))
    changed = plan.model_copy(deep=True)
    assert changed.leave_at is not None
    assert changed.prep_at is not None
    changed.leave_at += timedelta(minutes=6)
    changed.prep_at += timedelta(minutes=6)

    def planner(*args: Any, **kwargs: Any) -> Plan:
        nonlocal current
        current += timedelta(minutes=6)
        if during == "arrive":
            store.upsert_current_location(fix(captured_at=current))
        elif during == "stale":
            store.upsert_current_location(fix())
        elif during == "future_becomes_current":
            store.upsert_current_location(fix(captured_at=NOW + timedelta(minutes=5)))
        else:
            concurrent = plan.model_copy(deep=True)
            concurrent.event.title = "Concurrent edit"
            store.upsert_plan(concurrent)
        return changed

    notifier, alarm = poll(config, store, plan_event_fn=planner, now_fn=lambda: current)
    if during in {"arrive", "future_becomes_current", "cas"}:
        notifier.send.assert_not_called()
        assert store.get_plan(plan.event.id) != changed
        assert store.pending_pings(current) == []
    else:
        assert notifier.send.call_count == 1
        assert store.get_plan(plan.event.id) == changed
    alarm.send.assert_not_called()


def test_due_live_clock_after_blocking_send(config: Config, store: Store) -> None:
    config.home_assistant.replan_window_minutes = 0
    schedule(store, plan_at())
    store.upsert_current_location(fix())
    store.schedule_ping(PingEntry(id="first", event_id="digest", kind="digest",
                                  fire_at=NOW - timedelta(minutes=2), message="digest"))
    current = NOW
    notifier = MagicMock()

    def send(message: str) -> bool:
        nonlocal current
        current += timedelta(minutes=6)
        return True

    notifier.send.side_effect = send
    poll(config, store, notifier=notifier, now_fn=lambda: current)
    assert notifier.send.call_count == 3  # Presence expires while the digest blocks.


def test_stored_genuine_fix_survives_unavailable_fetch(config: Config, store: Store) -> None:
    schedule(store, plan_at())
    store.upsert_current_location(fix(captured_at=NOW - timedelta(minutes=4)))
    notifier, _ = poll(config, store, ha_fetch_fn=lambda *a, **kw: None)
    notifier.send.assert_not_called()
    assert len(store.pending_pings(NOW)) == 2
