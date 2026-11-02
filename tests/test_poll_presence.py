"""Ephemeral HA destination presence: atomic deferral, not persisted arrival state."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Literal
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
    assert notifier.send.call_count == 1
    assert alarm.send.call_count == 1
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
        "digest", "leave", "service_update",
    ]
    assert alarm.send.call_count == 1
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
    assert notifier.send.call_count == 1
    assert alarm.send.call_count == 1


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
    assert notifier.send.call_count == 1


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
    if during == "stale":
        # The reconciled prep is due; it replaces the standalone update.
        assert "Start getting ready" in notifier.send.call_args.args[0]
        alarm.send.assert_called_once()
    else:
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
    assert notifier.send.call_count == 2  # Presence expires; obsolete prep stays silent.


def test_stored_genuine_fix_survives_unavailable_fetch(config: Config, store: Store) -> None:
    schedule(store, plan_at())
    store.upsert_current_location(fix(captured_at=NOW - timedelta(minutes=4)))
    notifier, _ = poll(config, store, ha_fetch_fn=lambda *a, **kw: None)
    notifier.send.assert_not_called()
    assert len(store.pending_pings(NOW)) == 2


@pytest.mark.parametrize("order", [("prep", "leave"), ("leave", "prep")])
@pytest.mark.parametrize("equal", [False, True])
def test_due_actions_coalesce_independent_of_order(
    config: Config, store: Store,
    order: tuple[Literal["prep", "leave"], Literal["prep", "leave"]], equal: bool,
) -> None:
    config.home_assistant.replan_window_minutes = 0
    plan = plan_at()
    store.upsert_plan(plan)
    for kind in order:
        store.schedule_ping(PingEntry(
            id=kind, event_id=plan.event.id, kind=kind, message=kind,
            fire_at=NOW - timedelta(minutes=1 if equal or kind == "leave" else 2),
        ))
    # Exercise candidate order separately from chronological ordering.
    pending = store.pending_pings
    with patch.object(store, "pending_pings", side_effect=lambda before: sorted(
        pending(before), key=lambda p: order.index(p.kind),
    )):
        notifier, alarm = poll(config, store)
    assert [c.args[0] for c in notifier.send.call_args_list] == ["leave"]
    assert [c.args[0] for c in alarm.send.call_args_list] == ["leave"]
    assert store.pending_pings(NOW) == []


def test_future_leave_permits_prep_then_leave(config: Config, store: Store) -> None:
    config.home_assistant.replan_window_minutes = 0
    plan = plan_at()
    store.upsert_plan(plan)
    deadlines: tuple[tuple[Literal["prep", "leave"], datetime], ...] = (
        ("prep", NOW), ("leave", NOW + timedelta(minutes=10)),
    )
    for kind, deadline in deadlines:
        store.schedule_ping(PingEntry(id=kind, event_id=plan.event.id, kind=kind,
                                      fire_at=deadline, message=kind))
    notifier, alarm = poll(config, store)
    notifier.send.assert_called_once_with("prep")
    alarm.send.assert_called_once_with("prep")
    notifier, alarm = poll(config, store, now_fn=lambda: NOW + timedelta(minutes=10))
    notifier.send.assert_called_once_with("leave")
    alarm.send.assert_called_once_with("leave")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_phase5_imminent_dispatches_once_without_update(
    config: Config, store: Store, existing: bool, fails: bool,
) -> None:
    old = plan_at().model_copy(update={"prep_at": NOW + timedelta(minutes=5)})
    store.upsert_plan(old)
    if existing:
        for kind in ("prep", "leave"):
            store.schedule_ping(PingEntry(
                id=kind, event_id=old.event.id, kind=kind,
                fire_at=getattr(old, f"{kind}_at"), message=f"old {kind}",
            ))
    updated = old.model_copy(update={
        "prep_at": NOW - timedelta(minutes=25), "leave_at": NOW - timedelta(minutes=5),
        "error": "too_imminent",
    })
    primary, alarm = MagicMock(), MagicMock()
    primary.send.return_value = not fails
    poll(config, store, notifier=primary, ha_alarm_notifier=alarm,
         plan_event_fn=lambda *a, **kw: updated)
    primary.send.assert_called_once()
    assert "Leave now" in primary.send.call_args.args[0]
    assert "no prep window" in primary.send.call_args.args[0]
    assert "Location update" not in primary.send.call_args.args[0]
    if fails:
        alarm.send.assert_not_called()
        pending = store.pending_pings(NOW)
        assert len(pending) == 1 and pending[0].kind == "leave"
        assert pending[0].send_attempts == 1
        retry = pending[0]
        poll(config, store, notifier=primary, ha_alarm_notifier=alarm)
        assert primary.send.call_count == 2
        again = store.pending_pings(NOW)[0]
        assert (again.id, again.fire_at, again.message) == (retry.id, retry.fire_at, retry.message)
        assert again.send_attempts == 2
        alarm.send.assert_not_called()
    else:
        alarm.send.assert_called_once()
        poll(config, store, notifier=primary, ha_alarm_notifier=alarm)
        primary.send.assert_called_once()
        alarm.send.assert_called_once()
        assert store.pending_pings(NOW) == []


def test_consumed_leave_suppresses_imminent_location_update(config: Config, store: Store) -> None:
    old = plan_at()
    store.upsert_plan(old)
    store.schedule_ping(PingEntry(id="leave", event_id=old.event.id, kind="leave",
                                  fire_at=NOW - timedelta(minutes=1), message="leave"))
    assert store.claim_ping("leave", NOW)
    updated = old.model_copy(update={
        "prep_at": NOW - timedelta(minutes=20), "leave_at": NOW - timedelta(minutes=1),
        "error": "too_imminent",
    })
    notifier, alarm = poll(config, store, plan_event_fn=lambda *a, **kw: updated)
    notifier.send.assert_not_called()
    alarm.send.assert_not_called()
    assert store.pending_pings(NOW) == []


@pytest.mark.parametrize("first", ["prep", "leave"])
def test_atomic_suppression_across_store_instances(
    config: Config, store: Store, first: Literal["prep", "leave"],
) -> None:
    schedule(store, plan_at(), retry=2)
    before = {p.kind: p for p in store.pending_pings(NOW)}
    other = Store(config.paths.db_path)
    for kind, connection in ((first, store), ("leave" if first == "prep" else "prep", other)):
        claimed = connection.claim_ping_entry(
            before[kind].id, NOW, due_only=True, suppress_obsolete_prep=True,
        )
        assert (claimed is not None) == (kind == "leave")
    with store._connect() as conn:
        rows = conn.execute("SELECT id, fire_at, message, send_attempts, fired FROM pings").fetchall()
    for row in rows:
        original = next(p for p in before.values() if p.id == row[0])
        assert row[1:] == (original.fire_at.isoformat(), original.message, 2, 1)


def test_live_leave_postponement_permits_prep_and_current_payload(config: Config, store: Store) -> None:
    schedule(store, plan_at())
    snapshot = store.pending_pings(NOW)
    other = Store(config.paths.db_path)
    with other._connect() as conn:
        conn.execute("UPDATE pings SET fire_at = ? WHERE kind = 'leave'",
                     ((NOW + timedelta(minutes=10)).isoformat(),))
        conn.execute("UPDATE pings SET message = 'current prep' WHERE kind = 'prep'")
    claimed = store.claim_ping_entry(
        next(p.id for p in snapshot if p.kind == "prep"), NOW,
        due_only=True, suppress_obsolete_prep=True,
    )
    assert claimed is not None and claimed.message == "current prep"
    assert store.claim_ping_entry(
        next(p.id for p in snapshot if p.kind == "leave"), NOW,
        due_only=True, suppress_obsolete_prep=True,
    ) is None
    assert other.get_pending_ping(plan_at().event.id, "leave") is not None


def test_replan_outcome_respects_final_offset_deadlines(config: Config, store: Store) -> None:
    old = plan_at().model_copy(update={"prep_at": NOW + timedelta(minutes=5)})
    store.upsert_plan(old)
    # Positive user offsets keep both final alarms in the future even when the
    # replanned nominal departure is already past.
    for kind in ("prep", "leave"):
        store.schedule_ping(PingEntry(
            id=kind, event_id=old.event.id, kind=kind,
            fire_at=getattr(old, f"{kind}_at") + timedelta(minutes=60), message=kind,
        ))
    updated = old.model_copy(update={
        "prep_at": NOW - timedelta(minutes=20), "leave_at": NOW - timedelta(minutes=1),
        "error": "too_imminent",
    })
    outcome = store.reconcile_poll_replan(old, updated, NOW)
    assert outcome is not None
    assert not outcome.actionable_due and not outcome.imminent_leave_consumed
    assert not store.pending_pings(NOW)
    assert store.reconcile_poll_replan(old, updated, NOW) is None


@pytest.mark.parametrize("fold", [0, 1])
def test_pending_and_suppression_compare_instants_across_dst_folds(
    store: Store, fold: int,
) -> None:
    from datetime import UTC

    now = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=fold)
    past = now.astimezone(UTC) - timedelta(minutes=1)
    future = now.astimezone(UTC) + timedelta(minutes=1)
    # Mix offsets such that lexicographic ISO comparison gets both membership
    # and ordering wrong. All dates must instead be compared as instants.
    store.schedule_ping(PingEntry(id="prep", event_id="dst", kind="prep",
                                  fire_at=past, message="prep"))
    store.schedule_ping(PingEntry(id="leave", event_id="dst", kind="leave",
                                  fire_at=past.astimezone(NYC_TZ), message="leave"))
    store.schedule_ping(PingEntry(id="future", event_id="later", kind="prep",
                                  fire_at=future.astimezone(NYC_TZ), message="future"))
    assert {p.id for p in store.pending_pings(now)} == {"prep", "leave"}
    assert store.claim_ping_entry("prep", now, due_only=True, suppress_obsolete_prep=True) is None
    assert store.claim_ping_entry("leave", now, due_only=True, suppress_obsolete_prep=True) is not None
    assert store.get_pending_ping("later", "prep") is not None


def test_released_leave_not_retried_by_second_dispatch(config: Config, store: Store) -> None:
    # A deliberately offset due leave is eligible for early dispatch while the
    # nominal plan still qualifies for a phase-5 location refresh.
    old = plan_at()
    schedule(store, old)
    updated = old.model_copy(update={
        "prep_at": NOW - timedelta(minutes=20), "leave_at": NOW - timedelta(minutes=1),
        "error": "too_imminent",
    })
    primary, alarm = MagicMock(), MagicMock()
    primary.send.return_value = False
    poll(config, store, notifier=primary, ha_alarm_notifier=alarm,
         plan_event_fn=lambda *a, **kw: updated)
    primary.send.assert_called_once_with("leave")
    alarm.send.assert_not_called()
    pending = store.pending_pings(NOW)
    assert len(pending) == 1 and pending[0].kind == "leave"
    assert pending[0].id == f"{old.event.id}-leave"
    # Reconciliation advances the old -11 minute user offset with the plan;
    # a second send would incorrectly increment attempts within this invocation.
    assert pending[0].fire_at == NOW - timedelta(minutes=12)
    assert pending[0].send_attempts == 1


def test_quiet_prep_untouched_then_retired_when_eligible(config: Config, store: Store) -> None:
    config.home_assistant.replan_window_minutes = 0
    config.scheduling.quiet_hours_start = (NOW - timedelta(hours=1)).timetz()
    config.scheduling.quiet_hours_end = (NOW + timedelta(minutes=5)).timetz()
    schedule(store, plan_at(), retry=2)
    before = store.get_pending_ping(plan_at().event.id, "prep")
    notifier, alarm = poll(config, store)
    notifier.send.assert_called_once_with("leave")
    alarm.send.assert_called_once_with("leave")
    assert store.get_pending_ping(plan_at().event.id, "prep") == before
    notifier, alarm = poll(config, store, now_fn=lambda: NOW + timedelta(minutes=6))
    notifier.send.assert_not_called()
    alarm.send.assert_not_called()
    assert not store.pending_pings(NOW + timedelta(minutes=6))


def test_retry_cap_retires_only_leave_without_ha(config: Config, store: Store) -> None:
    config.home_assistant.replan_window_minutes = 0
    schedule(store, plan_at(), retry=5)
    notifier, alarm = MagicMock(), MagicMock()
    notifier.send.return_value = False
    poll(config, store, notifier=notifier, ha_alarm_notifier=alarm)
    # Existing policy allows the final attempt, but does not release it again.
    notifier.send.assert_called_once_with("leave")
    alarm.send.assert_not_called()
    assert not store.pending_pings(NOW)


def test_pending_order_is_instant_order_not_iso_order(store: Store) -> None:
    from datetime import UTC

    deadlines = [
        datetime(2026, 11, 1, 1, 20, tzinfo=NYC_TZ, fold=1),
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=1),
    ]
    for index, deadline in enumerate(deadlines):
        store.schedule_ping(PingEntry(id=str(index), event_id=str(index), kind="leave",
                                      fire_at=deadline, message="leave"))
    assert [p.id for p in store.pending_pings(NOW)] == ["1", "2", "0"]
