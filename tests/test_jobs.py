"""Tests for jobs (morning + poll)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Protocol, cast
from unittest.mock import MagicMock, patch

import pytest

from commutecompass.jobs.morning import run as morning_run
from commutecompass.jobs.poll import run as poll_run
from commutecompass.config import (
    CalendarSpec,
    Config,
    Origin,
    PathsConfig,
    PrepConfig,
    SchedulingConfig,
    OpencodeGoConfig,
    MtaConfig,
)
from commutecompass.models import (
    Alert,
    Event,
    PingEntry,
    Plan,
    Route,
    TransitLeg,
)
from commutecompass.store import Store
from commutecompass.timeutil import NYC_TZ, now_nyc
from commutecompass.notify import TelegramNotifier
from commutecompass.realtime import FetchResult, Prediction


# ─────────── Fixtures ──────────────────────────────────────────────────────────

def refresh_fixture(store: Store, now: datetime, *, leave_minutes: int = 30) -> Plan:
    """Persist a morning-style plan with independently validated GTFS identity."""
    boarding = now + timedelta(minutes=leave_minutes + 5)
    leg = TransitLeg(
        mode="TRANSIT", system="MTA Subway", line="Q", depart_at=boarding,
        arrive_at=boarding + timedelta(minutes=20), duration_seconds=1200,
        summary="Q", departure_stop="14 St-Union Sq", scheduled_departure_valid=True,
        gtfs_trip_id="trip", gtfs_route_id="Q", gtfs_start_date=now.strftime("%Y%m%d"),
        gtfs_start_time="07:00:00", gtfs_direction_id=0,
        gtfs_boarding_stop_id="R20N", gtfs_boarding_stop_sequence=10,
    )
    event = Event(id="refresh", calendar_id="cal", calendar_name="Test", title="Class",
                  start=boarding + timedelta(minutes=40), end=boarding + timedelta(hours=2))
    plan = Plan(event=event, route=Route(legs=[leg], depart_at=boarding,
                                       arrive_at=leg.arrive_at, total_duration_seconds=1200),
                leave_at=now + timedelta(minutes=leave_minutes),
                prep_at=now + timedelta(minutes=leave_minutes - 10),
                leave_buffer_minutes=12,
                weather_buffer_minutes=7, weather_reason="rain")
    store.upsert_plan(plan)
    for kind, fire_at in [("prep", plan.prep_at), ("leave", plan.leave_at)]:
        assert fire_at is not None
        store.schedule_ping(PingEntry(id=kind, event_id=event.id, kind=cast(Any, kind),
                                      fire_at=fire_at, message=kind))
    return plan


def refresh_feed(plan: Plan, now: datetime, minutes: int) -> FetchResult:
    assert plan.route is not None
    leg = plan.route.legs[0]
    pred = Prediction(
        stop_id="R20N", route_id="Q", trip_id="trip", start_date=now.strftime("%Y%m%d"),
        start_time="07:00:00", direction_id=0, stop_sequence=10,
        departure=leg.depart_at + timedelta(minutes=minutes), arrival=None,
        delay=minutes * 60, trip_relationship=0, stop_relationship=0,
        header_timestamp=int(now.timestamp()), update_timestamp=int(now.timestamp()), fresh=True,
    )
    return FetchResult({"R20N": [pred]}, usable_feeds=1)


@pytest.mark.parametrize("age,observed", [(299, False), (298, True)])
def test_poll_live_freshness_and_postfetch_urgent_clamp(
    minimal_config: Config, store: Store, age: int, observed: bool,
) -> None:
    start = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    current = start
    minimal_config.realtime.enabled = True
    plan = refresh_fixture(store, start, leave_minutes=11)
    feed = refresh_feed(plan, start - timedelta(seconds=age), 6)
    before = store.get_pending_ping(plan.event.id, "prep")
    assert before is not None
    notifier = MagicMock()
    notifier.send.return_value = True

    def fetch(*args: Any) -> FetchResult:
        nonlocal current
        current += timedelta(seconds=2)
        return feed

    with patch("commutecompass.realtime._cached_fetch", side_effect=fetch):
        poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: current,
                 fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    saved = store.get_plan(plan.event.id)
    assert saved is not None
    with store._connect() as conn:
        row = conn.execute("SELECT fire_at, fired FROM pings WHERE id='prep'").fetchone()
    if observed:
        assert saved.realtime_buffer_minutes == 6
        assert datetime.fromisoformat(row[0]) == start + timedelta(seconds=2)
        assert row[1] == 1
        notifier.send.assert_called_once()
    else:
        assert saved == plan
        assert datetime.fromisoformat(row[0]) == before.fire_at and row[1] == 0
        leave = store.get_pending_ping(plan.event.id, "leave")
        assert leave is not None and leave.fire_at == plan.leave_at
        notifier.send.assert_not_called()


def test_poll_ping_becoming_due_during_fetch_is_claimed_once(
    minimal_config: Config, store: Store,
) -> None:
    start = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    current = start
    minimal_config.realtime.enabled = True
    plan = refresh_fixture(store, start)
    due = start + timedelta(seconds=1)
    store.schedule_ping(PingEntry(id="prep", event_id=plan.event.id, kind="prep",
                                  fire_at=due, message="newly due"))
    notifier = MagicMock()
    notifier.send.return_value = True

    def fetch(*args: Any) -> FetchResult:
        nonlocal current
        current += timedelta(seconds=2)
        return FetchResult(failures=1)

    with patch("commutecompass.realtime._cached_fetch", side_effect=fetch), patch.object(
        store, "claim_ping_entry", wraps=store.claim_ping_entry,
    ) as claim:
        for _ in range(2):
            poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: current,
                     fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    notifier.send.assert_called_once_with("newly due")
    claim.assert_called_once_with(
        "prep", start + timedelta(seconds=2), leave_only=False, due_only=True,
    )


@pytest.mark.parametrize("blocking_stage", ["fetch", "send"])
def test_poll_retry_grace_uses_elapsed_network_time(
    minimal_config: Config, store: Store, blocking_stage: str,
) -> None:
    start = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    current = start
    minimal_config.realtime.enabled = True
    plan = refresh_fixture(store, start)
    store.schedule_ping(PingEntry(id="prep", event_id=plan.event.id, kind="prep",
                                  fire_at=start - timedelta(seconds=899), message="retry"))

    def fetch(*args: Any) -> FetchResult:
        nonlocal current
        if blocking_stage == "fetch":
            current += timedelta(seconds=2)
        return FetchResult(failures=1)

    def send(message: str) -> bool:
        nonlocal current
        if blocking_stage == "send":
            current += timedelta(seconds=2)
        return False

    notifier = MagicMock()
    notifier.send.side_effect = send
    with patch("commutecompass.realtime._cached_fetch", side_effect=fetch):
        for _ in range(2):
            poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: current,
                     fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    notifier.send.assert_called_once_with("retry")
    with store._connect() as conn:
        assert conn.execute("SELECT fired, send_attempts FROM pings WHERE id='prep'").fetchone() == (1, 0)


@pytest.mark.parametrize("minutes,leave_minutes,fired_kind", [
    (2, 30, None), (6, 3, None), (6, 30, "prep"), (6, 30, "leave"),
])
def test_poll_refresh_advances_in_place_before_dispatch(
    minimal_config: Config, store: Store, minutes: int, leave_minutes: int,
    fired_kind: Optional[str],
) -> None:
    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    minimal_config.realtime.enabled = True
    plan = refresh_fixture(store, now, leave_minutes=leave_minutes)
    if fired_kind:
        store.claim_ping(fired_kind, now)
    # Existing delivery failures must survive refresh.
    other = "leave" if fired_kind != "leave" else "prep"
    store.claim_ping(other, now)
    store.release_ping(other)
    notifier = MagicMock()
    notifier.send.return_value = True
    with patch("commutecompass.realtime._cached_fetch", return_value=refresh_feed(plan, now, minutes)):
        for _ in range(2):
            poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: now,
                     fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [],
                     plan_event_fn=MagicMock(side_effect=AssertionError("no Google replan")))
    updated = store.get_plan(plan.event.id)
    assert updated is not None and updated.leave_at is not None and plan.leave_at is not None
    assert updated.leave_at.timestamp() == plan.leave_at.timestamp() - minutes * 60
    assert updated.weather_buffer_minutes == 7 and updated.weather_reason == "rain"
    assert updated.realtime_buffer_minutes == minutes
    assert updated.leave_buffer_minutes == 12 + minutes
    with store._connect() as conn:
        rows = conn.execute("SELECT id, kind, fire_at, fired, send_attempts FROM pings ORDER BY id").fetchall()
    assert len(rows) == 2 and {r[0] for r in rows} == {"prep", "leave"}
    for ping_id, kind, fire_at, fired, attempts in rows:
        if kind == fired_kind:
            assert fired == 1
            original = plan.prep_at if kind == "prep" else plan.leave_at
            assert original is not None and datetime.fromisoformat(fire_at) == original
        else:
            original = plan.prep_at if kind == "prep" else plan.leave_at
            assert original is not None
            target = min(original.timestamp(), max(now.timestamp(), original.timestamp() - minutes * 60))
            assert datetime.fromisoformat(fire_at).timestamp() == target
        assert attempts == (1 if ping_id == other else 0)
    assert notifier.send.call_count == (2 if leave_minutes == 3 else 0)


@pytest.mark.parametrize("case", ["disabled", "distant", "unmatched", "unavailable", "smaller", "past", "cancelled"])
def test_poll_refresh_keeps_safety_padding_on_non_actionable_observations(
    minimal_config: Config, store: Store, case: str,
) -> None:
    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    minimal_config.realtime.enabled = case != "disabled"
    plan = refresh_fixture(store, now, leave_minutes=90 if case == "distant" else 30)
    feed = refresh_feed(plan, now, 2 if case == "smaller" else 6)
    plan.realtime_buffer_minutes = 4
    if case == "unmatched":
        assert plan.route is not None
        plan.route.legs[0].gtfs_trip_id = None
    if case == "past":
        plan.event.start = now - timedelta(minutes=1)
    if case == "cancelled":
        plan.error = "cancelled"
    if case == "unavailable":
        feed = FetchResult(failures=1)
    store.upsert_plan(plan)
    notifier = MagicMock()
    with patch("commutecompass.realtime._cached_fetch", return_value=feed) as fetch:
        poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: now,
                 fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    assert store.get_plan(plan.event.id) == plan
    if case in {"disabled", "distant", "past", "cancelled"}:
        fetch.assert_not_called()
    notifier.send.assert_not_called()


@pytest.mark.parametrize("source", ["alert", "ha"])
@pytest.mark.parametrize("send_ok", [True, False])
def test_poll_replan_cannot_rollback_refresh_or_recreate_prep(
    minimal_config: Config, store: Store, source: str, send_ok: bool,
) -> None:
    now = now_nyc().replace(hour=7, minute=30, second=0, microsecond=0)
    minimal_config.realtime.enabled = True
    minimal_config.home_assistant.enabled = source == "ha"
    plan = refresh_fixture(store, now, leave_minutes=25)
    plan.prep_at = now + timedelta(minutes=5)
    store.upsert_plan(plan)
    store.schedule_ping(PingEntry(id="prep", event_id=plan.event.id, kind="prep",
                                  fire_at=plan.prep_at, message="prep"))
    store.claim_ping("leave", now)
    store.release_ping("leave")
    directions = plan.model_copy(deep=True)
    assert directions.route is not None
    leg = directions.route.legs[0]
    for field in type(leg).model_fields:
        if field.startswith("gtfs_"):
            setattr(leg, field, None)
    leg.scheduled_departure_valid = False
    notifier = MagicMock()
    notifier.send.return_value = send_ok
    alert = Alert(id="rollback", header="Delay", description="Delay")
    planner = MagicMock(return_value=directions)
    with patch("commutecompass.realtime._cached_fetch", return_value=refresh_feed(plan, now, 10)):
        poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: now,
                 fetch_alerts_fn=lambda **kw: [alert] if source == "alert" else [],
                 select_alerts_fn=lambda alerts, *args, **kw: alerts,
                 plan_event_fn=planner, ha_fetch_fn=lambda *args, **kw: None,
                 ha_zones_fn=lambda *args, **kw: {})
    planner.assert_called_once()
    saved = store.get_plan(plan.event.id)
    assert saved is not None
    assert saved.realtime_buffer_minutes == 10
    assert saved.leave_at == now + timedelta(minutes=15)
    assert saved.prep_at == now - timedelta(minutes=5)
    assert saved.weather_buffer_minutes == 7
    with store._connect() as conn:
        rows = conn.execute("SELECT id, fired, send_attempts FROM pings ORDER BY id").fetchall()
    assert rows == [("leave", 0, 1), ("prep", int(send_ok), int(not send_ok))]
    assert notifier.send.call_count == 1  # No service update or recreated prep.


@pytest.mark.parametrize("source", ["alert", "ha"])
def test_poll_reparsed_walking_placeholders_retain_refresh_padding(
    minimal_config: Config, store: Store, source: str,
) -> None:
    import json
    from commutecompass.routing import _parse_route

    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    minimal_config.realtime.enabled = True
    minimal_config.home_assistant.enabled = source == "ha"
    plan = refresh_fixture(store, now, leave_minutes=25)
    sample = json.loads((Path(__file__).parent / "fixtures" / "directions_sample.json").read_text())
    provider_leg = sample["routes"][0]["legs"][0]
    for step in provider_leg["steps"]:
        if step["travel_mode"] == "WALKING":
            step.pop("departure_time", None)
            step.pop("arrival_time", None)
    details = provider_leg["steps"][1]["transit_details"]
    details["departure_time"]["value"] = int((now + timedelta(minutes=30)).timestamp())
    details["arrival_time"]["value"] = int((now + timedelta(minutes=50)).timestamp())
    details["line"]["short_name"] = "Q"
    details["departure_stop"]["name"] = "14 St-Union Sq"
    provider_leg["departure_time"]["value"] = int((now + timedelta(minutes=25)).timestamp())
    provider_leg["arrival_time"]["value"] = int((now + timedelta(minutes=53)).timestamp())

    def parse_at(observed: datetime) -> Route:
        with patch("commutecompass.routing.datetime", wraps=datetime) as clock:
            clock.now.return_value = observed
            route = _parse_route(sample)
        assert route is not None
        return route

    plan.route = parse_at(now)
    directions = plan.model_copy(deep=True)
    directions.route = parse_at(now + timedelta(minutes=1))
    assert plan.route.legs[0].depart_at != directions.route.legs[0].depart_at
    assert all(leg.gtfs_trip_id is None for leg in directions.route.legs)
    # Only the persisted fixture has independently validated boarding identity.
    identity = refresh_fixture(store, now, leave_minutes=25)
    assert identity.route is not None
    for field in type(identity.route.legs[0]).model_fields:
        if field.startswith("gtfs_"):
            setattr(plan.route.legs[1], field, getattr(identity.route.legs[0], field))
    store.upsert_plan(plan)
    notifier = MagicMock()
    notifier.send.return_value = True
    alert = Alert(id="walking-rollback", header="Delay", description="Delay")
    planner = MagicMock(return_value=directions)
    with patch("commutecompass.realtime._cached_fetch", return_value=refresh_feed(identity, now, 10)):
        poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: now,
                 fetch_alerts_fn=lambda **kw: [alert] if source == "alert" else [],
                 select_alerts_fn=lambda alerts, *args, **kw: alerts,
                 plan_event_fn=planner, ha_fetch_fn=lambda *args, **kw: None,
                 ha_zones_fn=lambda *args, **kw: {})
    planner.assert_called_once()
    saved = store.get_plan(plan.event.id)
    assert saved is not None and saved.realtime_buffer_minutes == 10
    assert saved.leave_at == now + timedelta(minutes=15)
    pending = store.get_pending_ping(plan.event.id, "leave")
    assert pending is not None and pending.id == "leave" and pending.fire_at == saved.leave_at
    notifier.send.assert_not_called()


@pytest.mark.parametrize("source", ["alert", "ha"])
def test_poll_discards_replan_when_refresh_commits_during_network(
    minimal_config: Config, store: Store, source: str,
) -> None:
    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    minimal_config.home_assistant.enabled = source == "ha"
    plan = refresh_fixture(store, now, leave_minutes=25)
    changed = plan.model_copy(deep=True)
    assert changed.route is not None and changed.leave_at is not None
    changed.route.legs[0].line = "N"
    changed.leave_at -= timedelta(minutes=10)

    def replan(*args: Any, **kwargs: Any) -> Plan:
        assert store.increase_realtime_buffer(plan, 6, "Q late", now)
        return changed

    notifier = MagicMock()
    alert = Alert(id="stale", header="Delay", description="Delay")
    poll_run(minimal_config, store=store, notifier=notifier, now_fn=lambda: now,
             fetch_alerts_fn=lambda **kw: [alert] if source == "alert" else [],
             select_alerts_fn=lambda alerts, *args, **kw: alerts,
             plan_event_fn=replan, ha_fetch_fn=lambda *args, **kw: None,
             ha_zones_fn=lambda *args, **kw: {})
    saved = store.get_plan(plan.event.id)
    assert saved is not None and saved.route is not None
    assert saved.realtime_buffer_minutes == 6 and saved.route.legs[0].line == "Q"
    pending = store.get_pending_ping(plan.event.id, "leave")
    assert pending is not None and pending.id == "leave" and pending.fire_at == saved.leave_at
    assert not store.is_alert_seen(alert.id, plan.event.id)
    notifier.send.assert_not_called()


@pytest.mark.parametrize("source", ["alert", "ha"])
@pytest.mark.parametrize("case", ["finished", "equal", "elapsed_during_fetch"])
def test_poll_replan_does_not_create_alarms_for_non_upcoming_event(
    minimal_config: Config, store: Store, source: str, case: str,
) -> None:
    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    live_now = now
    minimal_config.home_assistant.enabled = source == "ha"
    plan = refresh_fixture(store, now, leave_minutes=10)
    store.cancel_pings(plan.event.id)
    if case == "finished":
        plan.event.start = now - timedelta(hours=3)
        plan.event.end = now - timedelta(hours=2)
        if source == "alert":
            plan.leave_at = plan.event.start - timedelta(minutes=45)
            plan.prep_at = plan.leave_at - timedelta(minutes=20)
        # HA keeps a stale future leave_at to exercise its otherwise reachable window.
    elif case == "equal":
        plan.event.start = now
        plan.event.end = now + timedelta(hours=1)
    store.upsert_plan(plan)
    changed = plan.model_copy(deep=True)
    assert changed.route is not None
    changed.route.legs[0].line = "N"
    changed.leave_at = now - timedelta(minutes=1)
    changed.prep_at = now - timedelta(minutes=21)
    changed.error = "too_imminent"

    def replan(*args: Any, **kwargs: Any) -> Plan:
        nonlocal live_now
        live_now = plan.event.start  # Event starts while Google is blocking.
        return changed

    planner = MagicMock(side_effect=replan)
    notifier = MagicMock()
    ha_alarm = MagicMock()
    alert = Alert(id="expired", header="Delay", description="Delay")
    for _ in range(2):
        poll_run(minimal_config, store=store, notifier=notifier,
                 ha_alarm_notifier=ha_alarm, now_fn=lambda: live_now,
                 fetch_alerts_fn=lambda **kw: [alert] if source == "alert" else [],
                 select_alerts_fn=lambda alerts, *args, **kw: alerts,
                 plan_event_fn=planner, ha_fetch_fn=lambda *args, **kw: None,
                 ha_zones_fn=lambda *args, **kw: {})
    if case == "elapsed_during_fetch":
        planner.assert_called_once()
    else:
        planner.assert_not_called()
    assert store.get_plan(plan.event.id) == plan
    with store._connect() as conn:
        assert conn.execute("SELECT * FROM pings").fetchall() == []
    assert not store.is_alert_seen(alert.id, plan.event.id)
    notifier.send.assert_not_called()
    ha_alarm.send.assert_not_called()


def test_poll_reconciliation_preserves_fired_retry_offsets_and_new_due(store: Store) -> None:
    now = now_nyc()
    plan = refresh_fixture(store, now)
    assert plan.leave_at is not None
    store.claim_ping("prep", now)
    store.claim_ping("leave", now)
    store.release_ping("leave")
    store.schedule_ping(PingEntry(id="leave", event_id=plan.event.id, kind="leave",
                                  fire_at=plan.leave_at + timedelta(minutes=4), message="snoozed"))
    store.claim_ping("leave", now)
    store.release_ping("leave")
    changed = plan.model_copy(update={"leave_at": now - timedelta(minutes=1),
                                      "prep_at": now - timedelta(minutes=21)})
    assert store.reconcile_poll_replan(plan, changed, now)
    with store._connect() as conn:
        rows = conn.execute("SELECT id, fired, send_attempts, fire_at FROM pings ORDER BY id").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("leave", 0, 1), ("prep", 1, 0)]
    assert datetime.fromisoformat(rows[0][3]) == now + timedelta(minutes=3)
    assert not store.reconcile_poll_replan(plan, changed, now)
    # No existing kind: a past time must produce an immediately due row.
    store.cancel_pings(plan.event.id)
    assert store.reconcile_poll_replan(changed, changed, now)
    assert {p.kind for p in store.pending_pings(now)} == {"prep", "leave"}


def test_replan_safety_keeps_only_same_journey_padding_and_manual_interval(store: Store) -> None:
    from commutecompass.jobs.poll import _retain_replan_safety

    now = now_nyc()
    old = refresh_fixture(store, now)
    assert old.leave_at is not None
    old.realtime_buffer_minutes = 10
    old.realtime_reason = "Q late"
    old.leave_at -= timedelta(minutes=10)
    old.prep_at = old.leave_at - timedelta(minutes=35)  # Manual extra prep.
    new = old.model_copy(deep=True)
    new.realtime_buffer_minutes = 2
    new.leave_buffer_minutes = 19  # Safety 5 + weather 12 + realtime 2.
    new.realtime_reason = "Q less late"
    new.weather_buffer_minutes = 12  # Five additional weather minutes.
    new.leave_at = old.leave_at + timedelta(minutes=8 - 5)
    new.prep_at = new.leave_at - timedelta(minutes=20)
    assert new.route is not None
    new.route.legs[0].gtfs_trip_id = None
    new.route.legs[0].scheduled_departure_valid = False
    new.route.raw_provider_payload = {"different": True}
    retained = _retain_replan_safety(old, new)
    assert retained.leave_at is not None
    assert retained.leave_at == old.leave_at - timedelta(minutes=5)
    assert retained.prep_at == retained.leave_at - timedelta(minutes=35)
    assert retained.realtime_buffer_minutes == 10 and retained.realtime_reason == "Q late"
    assert retained.weather_buffer_minutes == 12
    assert retained.leave_buffer_minutes == 27
    assert retained.route is not None and retained.route.legs[0].gtfs_trip_id is None
    new.route.legs[0].line = "N"
    different = _retain_replan_safety(old, new)
    assert different.leave_at == new.leave_at
    assert different.realtime_buffer_minutes == 2 and different.realtime_reason == "Q less late"


@pytest.mark.parametrize("change", [
    "departure", "arrival", "route_departure", "route_arrival", "walk_duration", "boarding", "alighting",
])
def test_same_journey_rejects_real_parsed_changes(store: Store, change: str) -> None:
    import json
    from commutecompass.jobs.poll import _same_journey
    from commutecompass.routing import _parse_route

    sample = json.loads((Path(__file__).parent / "fixtures" / "directions_sample.json").read_text())
    old = refresh_fixture(store, now_nyc())
    old.route = _parse_route(sample)
    assert old.route is not None
    new = old.model_copy(deep=True)
    sample = json.loads(json.dumps(sample))
    steps = sample["routes"][0]["legs"][0]["steps"]
    if change in {"departure", "arrival"}:
        steps[1]["transit_details"][f"{change}_time"]["value"] += 60
    elif change.startswith("route_"):
        sample["routes"][0]["legs"][0][f"{change.removeprefix('route_')}_time"]["value"] += 60
    elif change == "walk_duration":
        steps[0]["duration"]["value"] += 60
    else:
        stop = "departure_stop" if change == "boarding" else "arrival_stop"
        steps[1]["transit_details"][stop]["name"] = "Different station"
    new.route = _parse_route(sample)
    assert not _same_journey(old, new)


@pytest.mark.parametrize("missing", ["route", "transit"])
def test_same_journey_ignores_missing_schedule_placeholders(store: Store, missing: str) -> None:
    import json
    from commutecompass.jobs.poll import _same_journey
    from commutecompass.routing import _parse_route

    sample = json.loads((Path(__file__).parent / "fixtures" / "directions_sample.json").read_text())
    provider_leg = sample["routes"][0]["legs"][0]
    target = provider_leg if missing == "route" else provider_leg["steps"][1]["transit_details"]
    target.pop("departure_time")
    target.pop("arrival_time")
    old = refresh_fixture(store, now_nyc())
    new = old.model_copy(deep=True)
    for plan, observed in [(old, now_nyc()), (new, now_nyc() + timedelta(minutes=1))]:
        with patch("commutecompass.routing.datetime", wraps=datetime) as clock:
            clock.now.return_value = observed
            plan.route = _parse_route(sample)
    assert _same_journey(old, new)


def test_same_journey_compares_schedule_instants_across_dst_fold(store: Store) -> None:
    from datetime import UTC
    from commutecompass.jobs.poll import _same_journey

    old = refresh_fixture(store, now_nyc())
    assert old.route is not None
    old.route.legs[0].depart_at = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0)
    new = old.model_copy(deep=True)
    assert new.route is not None
    new.route.legs[0].depart_at = old.route.legs[0].depart_at.astimezone(UTC)
    assert _same_journey(old, new)
    new.route.legs[0].depart_at = old.route.legs[0].depart_at.replace(fold=1)
    assert not _same_journey(old, new)


def test_parsed_directions_plan_cannot_advance_due_alarm(
    minimal_config: Config, store: Store,
) -> None:
    import json
    from dataclasses import replace
    from commutecompass.routing import _parse_route

    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    plan = refresh_fixture(store, now, leave_minutes=3)
    sample = json.loads((Path(__file__).parent / "fixtures" / "directions_sample.json").read_text())
    details = sample["routes"][0]["legs"][0]["steps"][1]["transit_details"]
    details["departure_stop"]["name"] = "Astoria-Ditmars Blvd"
    details["line"]["short_name"] = "N"
    details["departure_time"]["value"] = int((now + timedelta(minutes=8)).timestamp())
    route = _parse_route(sample)
    assert route is not None
    transit = next(leg for leg in route.legs if leg.mode == "TRANSIT")
    assert transit.scheduled_departure_valid and transit.gtfs_trip_id is None
    # Even a fresh prediction at the same named station/line must not supply
    # identity missing from the real parser's output.
    feed = refresh_feed(plan, now, 6)
    pred = feed.predictions.pop("R20N")[0]
    assert transit.line is not None
    feed.predictions["R01N"] = [
        replace(pred, stop_id="R01N", route_id=transit.line,
                departure=transit.depart_at + timedelta(minutes=6))
    ]
    plan.route = route
    store.upsert_plan(plan)
    notifier = MagicMock()
    with patch("commutecompass.realtime._cached_fetch", return_value=feed) as fetch:
        poll_run(minimal_config.model_copy(update={
            "realtime": minimal_config.realtime.model_copy(update={"enabled": True}),
        }), store=store, notifier=notifier, now_fn=lambda: now,
                 fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    fetch.assert_called_once()
    assert store.get_plan(plan.event.id) == plan
    # Prep was already due, but the leave alarm must not falsely become due.
    assert notifier.send.call_count == 1
    pending = store.get_pending_ping(plan.event.id, "leave")
    assert pending is not None and pending.fire_at == plan.leave_at


def test_realtime_refresh_cas_rejects_stale_observation_and_preserves_manual_offset(
    store: Store,
) -> None:
    now = now_nyc()
    plan = refresh_fixture(store, now)
    manual = plan.model_copy(update={"prep_at": now + timedelta(minutes=15)})
    store.upsert_plan(manual)
    assert not store.increase_realtime_buffer(plan, 6, "late", now)
    assert store.get_plan(plan.event.id) == manual
    assert store.increase_realtime_buffer(manual, 2, "late", now)
    assert not store.increase_realtime_buffer(manual, 2, "late", now)
    current = store.get_plan(plan.event.id)
    assert current is not None and current.prep_at == now + timedelta(minutes=13)
    pending = store.get_pending_ping(plan.event.id, "prep")
    assert pending is not None and pending.fire_at == now + timedelta(minutes=18)
    assert not store.increase_realtime_buffer(current, 1, "smaller", now)


def test_morning_saved_plan_then_serialized_update_refreshes_normal_poll(
    minimal_config: Config, store: Store,
) -> None:
    from google.transit import gtfs_realtime_pb2 as gtfs  # type: ignore[import-untyped]
    from commutecompass.realtime import Predictions, _accumulate

    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    plan = refresh_fixture(store, now)
    store.cancel_pings(plan.event.id)
    minimal_config.realtime.enabled = True
    with patch("commutecompass.jobs.morning.CalendarClient") as calendar, patch(
        "commutecompass.jobs.morning.plan_event", return_value=plan,
    ), patch("commutecompass.jobs.morning.fetch_alerts", return_value=[]), patch(
        "commutecompass.jobs.morning.build_notifier", return_value=MagicMock(),
    ), patch("commutecompass.jobs.morning.now_nyc", return_value=now - timedelta(hours=4)):
        calendar.return_value.fetch_events.return_value = [plan.event]
        morning_run(minimal_config)
    saved = store.get_plan(plan.event.id)
    assert saved is not None and saved.route is not None
    before = {kind: store.get_pending_ping(plan.event.id, kind) for kind in ("prep", "leave")}
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = int(now.timestamp())
    entity = feed.entity.add()
    entity.id = "trip"
    update = entity.trip_update
    update.timestamp = int(now.timestamp())
    update.trip.trip_id = "trip"
    update.trip.route_id = "Q"
    update.trip.start_date = now.strftime("%Y%m%d")
    update.trip.start_time = "07:00:00"
    update.trip.direction_id = 0
    boarding = update.stop_time_update.add()
    boarding.stop_id = "R20N"
    boarding.stop_sequence = 10
    boarding.departure.delay = 120
    boarding.departure.time = int(saved.route.legs[0].depart_at.timestamp()) + 120
    predictions: Predictions = {}
    usable = _accumulate(gtfs.FeedMessage.FromString(feed.SerializeToString()),
                         predictions, "MTA Subway", now=now)
    result = FetchResult(predictions, usable_feeds=int(usable))
    with patch("commutecompass.realtime._cached_fetch", return_value=result):
        poll_run(minimal_config, store=store, notifier=MagicMock(), now_fn=lambda: now,
                 fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    refreshed = store.get_plan(plan.event.id)
    assert refreshed is not None and refreshed.realtime_buffer_minutes == 2
    assert refreshed.weather_buffer_minutes == 7
    for kind, original in before.items():
        current = store.get_pending_ping(plan.event.id, kind)
        assert original is not None and current is not None
        assert current.id == original.id
        assert current.fire_at.timestamp() == original.fire_at.timestamp() - 120

@pytest.fixture
def minimal_config(tmp_path: Path) -> "Config":
    """Minimal config for testing."""
    db_path = tmp_path / "test.db"
    venues_path = tmp_path / "venues.yaml"
    oauth_path = tmp_path / "token.json"

    venues_path.write_text(
        """
- aliases: ["200 Example St", "Example School"]
  resolves_to:
    kind: address
    value: "200 Example St, New York, NY 10001"
    source: known_venues
"""
    )
    oauth_path.write_text("{}")

    return Config(
        origin=Origin(
            address="123 Example Ave, Brooklyn, NY 11201",
            lat=40.6950,
            lon=-73.9890,
            subway_station="Jay St-MetroTech",
            lirr_station="Atlantic Terminal",
        ),
        calendars=[
            CalendarSpec(id="test-cal", name="Test", enabled=True),
        ],
        prep=PrepConfig(prep_minutes=20, safety_buffer_minutes=5),
        scheduling=SchedulingConfig(
            morning_run_time=datetime.strptime("06:00", "%H:%M").time(),
            poll_interval_seconds=60,
        ),
        paths=PathsConfig(
            venues_file=str(venues_path),
            db_path=str(db_path),
            oauth_token_path=str(oauth_path),
        ),
        opencode_go=OpencodeGoConfig(
            endpoint="https://example/v1/chat/completions",
            model="deepseek-v4-flash",
        ),
        mta=MtaConfig(
            subway_alerts_url="https://subway-alerts.example",
            lirr_alerts_url="https://lirr-alerts.example",
            bus_alerts_url="https://bus-alerts.example",
        ),
        google_maps_api_key="test-key",
        google_oauth_client_secret_json="{}",
        telegram_bot_token="test-token",
        telegram_chat_id=12345,
        opencode_go_token="test-token",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db_path = tmp_path / "test.db"
    s = Store(db_path)
    s.init_schema()
    return s


@pytest.fixture
def wide_today_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen ``logical_day_bounds_nyc()`` so today-based store queries don't
    miss test events near the 02:00 NYC boundary.

    Several poll/morning tests construct events at ``now + timedelta(hours=3)``.
    When CI happens to run shortly after midnight NYC, ``now + 3h`` crosses
    the logical-day cutoff and ``store.today_plans()`` returns an empty list,
    causing time-of-day flakes. This fixture replaces the bounds with a
    ±24h window around real ``now_nyc()`` for the duration of the test.

    Store/job code imports ``logical_day_bounds_nyc`` lazily inside each
    method, so patching the attribute on ``commutecompass.timeutil`` is enough.
    """
    from commutecompass import timeutil

    def _wide_bounds(reference: Optional[datetime] = None, **_: Any) -> tuple[datetime, datetime]:
        anchor = timeutil.to_nyc(reference) if reference is not None else timeutil.now_nyc()
        return (anchor - timedelta(hours=24), anchor + timedelta(hours=24))

    monkeypatch.setattr(timeutil, "logical_day_bounds_nyc", _wide_bounds)


@pytest.fixture
def today_events() -> list[Event]:
    """Two events today: one with location, one without."""
    now = now_nyc()
    return [
        Event(
            id="evt-1",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Example Class",
            start=(now + timedelta(hours=3)).astimezone(NYC_TZ),
            end=(now + timedelta(hours=5)).astimezone(NYC_TZ),
            location_raw="200 Example St",
            location_resolved=None,
            mode_override=None,
        ),
        Event(
            id="evt-2",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Team Meeting",
            start=(now + timedelta(hours=6)).astimezone(NYC_TZ),
            end=(now + timedelta(hours=7)).astimezone(NYC_TZ),
            location_raw=None,
            location_resolved=None,
            mode_override=None,
        ),
    ]


@pytest.fixture
def sample_route() -> Route:
    """A sample transit route."""
    now = now_nyc()
    return Route(
        legs=[
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=now,
                arrive_at=now + timedelta(minutes=5),
                duration_seconds=300,
                summary="Walk to Jay St-MetroTech",
            ),
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St",
                depart_at=now + timedelta(minutes=5),
                arrive_at=now + timedelta(minutes=35),
                duration_seconds=1800,
                summary="C train to Fulton St",
            ),
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=now + timedelta(minutes=35),
                arrive_at=now + timedelta(minutes=40),
                duration_seconds=300,
                summary="Walk to destination",
            ),
        ],
        depart_at=now,
        arrive_at=now + timedelta(minutes=40),
        total_duration_seconds=2400,
        transfers=0,
        raw_provider_payload=None,
    )


# ─────────── Morning job tests ────────────────────────────────────────────────

def test_morning_run_surfaces_calendar_auth_failure(
    minimal_config: Config,
    tmp_path: Path,
) -> None:
    """An expired OAuth token must be reported in the digest, not silently empty."""
    from commutecompass.calendar_client import AuthError

    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.side_effect = AuthError("token refresh failed")
        mock_cal_class.return_value = mock_cal
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        morning_run(minimal_config)

    # Digest still sent, and it tells the user to re-auth.
    mock_notifier.send.assert_called_once()
    sent_text = mock_notifier.send.call_args.args[0]
    assert "oauth" in sent_text.lower()


def _run_morning_capture_digest(config: Config) -> str:
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = []
        mock_cal_class.return_value = mock_cal
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier
        morning_run(config)
        return str(mock_notifier.send.call_args.args[0])


def test_morning_flags_dead_poll_timer(minimal_config: Config) -> None:
    """A missing/stale poll heartbeat is surfaced in the digest."""
    # No poll heartbeat recorded → stale.
    text = _run_morning_capture_digest(minimal_config)
    assert "poll loop has not run" in text.lower()


def test_morning_silent_when_poll_heartbeat_fresh(minimal_config: Config) -> None:
    """A fresh poll heartbeat means no dead-timer warning."""
    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.record_job_success("poll", now_nyc())
    text = _run_morning_capture_digest(minimal_config)
    assert "poll loop has not run" not in text.lower()


def test_morning_run_fetches_and_plans(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Verify the full morning sequence: fetch → plan → upsert → ping schedule → digest."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        # ── CalendarClient mock ─────────────────────────────────────────
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        # ── fetch_alerts mock ───────────────────────────────────────────
        mock_fetch_alerts.return_value = []

        # ── TelegramNotifier mock ───────────────────────────────────────
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        # ── plan_event: return a successful plan for evt-1, error plan for evt-2 ─
        now = now_nyc()
        evt1_plan = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=(now + timedelta(hours=3)) - timedelta(minutes=45),
            prep_at=(now + timedelta(hours=3)) - timedelta(minutes=65),
        )
        evt2_plan = Plan(event=today_events[1], error="location_unresolved")

        def mock_plan_event(
            event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any
        ) -> Plan:
            if event.id == "evt-1":
                return evt1_plan
            return evt2_plan

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        # ── Verify calendar_client was called ───────────────────────────
        mock_cal.fetch_events.assert_called_once()
        call_args = mock_cal.fetch_events.call_args
        # Compare by identity since CalendarSpec is a pydantic model
        assert minimal_config.calendars[0].id == call_args.kwargs["calendars"][0].id
        assert minimal_config.calendars[0].name == call_args.kwargs["calendars"][0].name
        assert minimal_config.calendars[0].enabled == call_args.kwargs["calendars"][0].enabled

        # ── Verify the fetch window matches logical_day_bounds_nyc, not midnight ─
        from commutecompass.timeutil import logical_day_bounds_nyc

        expected_start, expected_end = logical_day_bounds_nyc(now)
        actual_start = call_args.kwargs["start"]
        actual_end = call_args.kwargs["end"]
        assert actual_start == expected_start, (
            f"fetch_events start={actual_start} should be logical day start {expected_start}"
        )
        assert actual_end == expected_end, (
            f"fetch_events end={actual_end} should be logical day end {expected_end}"
        )

        # ── Verify store: both plans upserted ───────────────────────────
        store = Store(minimal_config.paths.db_path)
        saved_plan1 = store.get_plan("evt-1")
        saved_plan2 = store.get_plan("evt-2")
        assert saved_plan1 is not None
        assert saved_plan2 is not None

        # ── Verify pings scheduled for evt-1 (evt-2 has no route/leave_at) ─
        pending = store.pending_pings(before=now + timedelta(days=1))
        ping_map = {p.event_id: p for p in pending}

        assert "evt-1" in ping_map
        prep_ping = next(p for p in pending if p.kind == "prep" and p.event_id == "evt-1")
        leave_ping = next(p for p in pending if p.kind == "leave" and p.event_id == "evt-1")
        assert prep_ping.fire_at == evt1_plan.prep_at
        assert leave_ping.fire_at == evt1_plan.leave_at

        # ── Verify no pings for evt-2 (error event) ─────────────────────
        evt2_pings = [p for p in pending if p.event_id == "evt-2"]
        assert evt2_pings == []

        # ── Verify digest was built and sent ─────────────────────────────
        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "Example Class" in digest_text
        assert "Team Meeting" in digest_text


def test_morning_run_skips_past_pings(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Pings with fire_at in the past are not scheduled."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        # Plan where leave_at is already in the past
        past_leave = now - timedelta(minutes=10)
        past_plan = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=past_leave,
            prep_at=past_leave - timedelta(minutes=20),
        )

        def mock_plan_event(event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any) -> Plan:
            return past_plan

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        store = Store(minimal_config.paths.db_path)
        pending = store.pending_pings(before=now + timedelta(days=1))
        # No pings should be scheduled because both prep_at and leave_at are in the past
        assert pending == []


def test_morning_run_idempotent(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Re-running morning replaces, not stacks, pending pings for each (event_id, kind).

    The dedup invariant: scheduling the same (event_id, kind) twice results in
    a single effective pending ping — the second run's values (fire_at/message)
    replace the first, without multiplying the ping row count.
    """
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal_class.return_value = mock_cal
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )
        plan2 = Plan(event=today_events[1], error="location_unresolved")

        call_count = 0

        def mock_plan_event(event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any) -> Plan:
            nonlocal call_count
            call_count += 1
            if event.id == "evt-1":
                return plan1
            return plan2

        # First run: fetch_events returns 2 events
        mock_cal.fetch_events.return_value = today_events

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        # Second run: same events — should overwrite cleanly
        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        # plan_event should have been called 4 times total (2 events × 2 runs)
        assert call_count == 4

        import sqlite3
        with sqlite3.connect(minimal_config.paths.db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM pings").fetchone()
            # Dedup invariant: one prep + one leave ping for evt-1 (evt-2 has no route → 0 pings).
            # Second run replaces the first run's pings, it does not stack — total = 2, not 4.
            assert rows[0] == 2


def test_morning_run_cancel_stale_pings(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
    wide_today_window: None,
) -> None:
    """Events removed from the calendar have their pings cancelled."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_fetch_alerts.return_value = []
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier
        mock_cal_class.return_value = mock_cal

        now = now_nyc()
        store = Store(minimal_config.paths.db_path)
        store.init_schema()

        # Pre-existing plan + pings for an event that will NOT appear today
        stale_event = Event(
            id="stale-evt",
            calendar_id="test-cal",
            calendar_name="Test",
            title="Old Rehearsal",
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=4),
            location_raw="200 Example St",
        )
        stale_route = sample_route
        stale_plan = Plan(
            event=stale_event,
            route=stale_route,
            leave_at=now + timedelta(hours=2) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=2) - timedelta(minutes=65),
        )
        store.upsert_plan(stale_plan)
        assert stale_plan.prep_at is not None
        assert stale_plan.leave_at is not None
        store.schedule_ping(
            PingEntry(
                id="stale-ping-1",
                event_id="stale-evt",
                kind="prep",
                fire_at=stale_plan.prep_at,
                message="old prep",
            )
        )
        store.schedule_ping(
            PingEntry(
                id="stale-ping-2",
                event_id="stale-evt",
                kind="leave",
                fire_at=stale_plan.leave_at,
                message="old leave",
            )
        )

        # First run's events — evt-1 but NOT the stale event
        mock_cal.fetch_events.return_value = [today_events[0]]

        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )

        def mock_plan_event(event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any) -> Plan:
            return plan1

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        # Stale pings should be gone
        remaining = store.pending_pings(before=now + timedelta(days=1))
        stale_remaining = [p for p in remaining if p.event_id == "stale-evt"]
        assert stale_remaining == []


def test_morning_run_with_affecting_alerts(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Digest includes affecting MTA alerts."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        # Return an alert affecting the C line
        alert = Alert(
            id="alert-c-1",
            header="C train delays",
            description="Expect delays on the C line",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[
                (now_nyc() - timedelta(hours=1), now_nyc() + timedelta(hours=4))
            ],
            severity="WARNING",
        )
        mock_fetch_alerts.return_value = [alert]

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )
        plan2 = Plan(event=today_events[1], error="location_unresolved")

        def mock_plan_event(event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any) -> Plan:
            if event.id == "evt-1":
                return plan1
            return plan2

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            morning_run(minimal_config)

        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "C train delays" in digest_text


def test_morning_run_telegram_failure_is_not_fatal(
    minimal_config: Config,
    tmp_path: Path,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Telegram send failure doesn't raise; it logs and continues."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = today_events
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = False  # Telegram failure
        mock_notifier_class.return_value = mock_notifier

        now = now_nyc()
        plan1 = Plan(
            event=today_events[0],
            route=sample_route,
            leave_at=now + timedelta(hours=3) - timedelta(minutes=45),
            prep_at=now + timedelta(hours=3) - timedelta(minutes=65),
        )

        def mock_plan_event(event: Event, config: Config, venues: Any, store: Any, llm: Any, *, mode_override: str | None = None, **_extra: Any) -> Plan:
            return plan1

        with patch("commutecompass.jobs.morning.plan_event", side_effect=mock_plan_event):
            # Should NOT raise
            morning_run(minimal_config)

        # Plans still persisted
        store = Store(minimal_config.paths.db_path)
        assert store.get_plan("evt-1") is not None


def test_morning_run_empty_calendar(
    minimal_config: Config,
    tmp_path: Path,
) -> None:
    """Empty calendar: digest sent with 'no events' message."""
    with patch("commutecompass.jobs.morning.CalendarClient") as mock_cal_class, patch(
        "commutecompass.jobs.morning.fetch_alerts"
    ) as mock_fetch_alerts, patch(
        "commutecompass.jobs.morning.build_notifier"
    ) as mock_notifier_class:
        mock_cal = MagicMock()
        mock_cal.fetch_events.return_value = []
        mock_cal_class.return_value = mock_cal

        mock_fetch_alerts.return_value = []

        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        mock_notifier_class.return_value = mock_notifier

        morning_run(minimal_config)

        mock_notifier.send.assert_called_once()
        digest_text = mock_notifier.send.call_args[0][0]
        assert "No events" in digest_text or "today" in digest_text.lower()


# ─────────── Poll job tests ────────────────────────────────────────────────────

class SpyNotifier:
    """Notifier that records sends and is configurable per-call."""

    def __init__(self, return_value: bool = True) -> None:
        self.sent: list[str] = []
        self._return_value = return_value

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self._return_value


class TelegramNotifierProto(Protocol):
    """Duck-typed notifier protocol matching TelegramNotifier.send signature."""

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> bool: ...


class MockPlanner:
    """Plan-event stand-in that returns a pre-configured plan or records the call."""

    def __init__(
        self,
        plan: Optional[Plan] = None,
        raise_on: Optional[Exception] = None,
    ) -> None:
        self.plan = plan
        self.raise_on = raise_on
        self.calls: list[Event] = []

    def __call__(self, event: Event, **kwargs: object) -> Plan:
        self.calls.append(event)
        if self.raise_on:
            raise self.raise_on
        assert self.plan is not None
        return self.plan


# ── Due ping fires once ────────────────────────────────────────────────────────

def test_poll_fires_due_ping_once(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """A due ping is sent exactly once; a second poll run does not refire."""
    now = now_nyc()

    # Build the plan and its leave_at
    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    # A leave ping that is already due
    due_ping = PingEntry(
        id="ping-due-1",
        event_id=today_events[0].id,
        kind="leave",
        fire_at=now - timedelta(minutes=5),
        fired=False,
        message="🚶 *Leave now* — Test Event",
    )

    # Persist the plan and ping
    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(due_ping)

    notifier = SpyNotifier()

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
    )

    # Ping should have been fired exactly once
    assert len(notifier.sent) == 1
    assert notifier.sent[0] == "🚶 *Leave now* — Test Event"

    # Running poll again should NOT fire the same ping (marked fired)
    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now + timedelta(minutes=1),
    )
    assert len(notifier.sent) == 1  # still exactly 1


# ── New alert triggers replan + service_update + alert seen ────────────────────

def test_poll_new_alert_triggers_replan_and_service_update(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
    wide_today_window: None,
) -> None:
    """A newly-affecting alert causes replan, service_update send, upsert, and mark-seen."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    original_plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    # New route with a later leave_at (15 min later)
    new_leave_at = leave_at + timedelta(minutes=15)
    new_prep_at = new_leave_at - timedelta(minutes=20)
    new_route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St (delayed)",
                depart_at=new_leave_at - timedelta(minutes=45),
                arrive_at=new_leave_at,
                duration_seconds=2700,
                summary="C train to Fulton St (delayed)",
            ),
        ],
        depart_at=new_leave_at - timedelta(minutes=45),
        arrive_at=new_leave_at,
        total_duration_seconds=2700,
        transfers=0,
    )
    replanned_plan = Plan(
        event=today_events[0],
        route=new_route,
        leave_at=new_leave_at,
        prep_at=new_prep_at,
    )

    alert = Alert(
        id="alert-c-new",
        header="C train delays",
        description="Expect delays on the C line due to signal problems.",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=2))],
        severity="WARNING",
        url=None,
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(original_plan)

    notifier = SpyNotifier()
    planner = MockPlanner(replanned_plan)

    # Alert is not yet marked seen
    assert not store.is_alert_seen(alert.id, today_events[0].id)

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [alert],
        alerts_affecting_route_fn=lambda alerts, route, at_time: [alert]
        if alert in alerts
        else [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
    )

    # Replan should have been called with the event
    assert len(planner.calls) == 1
    assert planner.calls[0].id == today_events[0].id

    # Service update should have been sent (some message about C line)
    assert any("C train" in s or "delays" in s for s in notifier.sent)

    # Alert should now be marked seen
    assert store.is_alert_seen(alert.id, today_events[0].id)


# ── Re-running poll does not refire ─────────────────────────────────────────

def test_poll_rerun_does_not_replan_or_resend(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Re-running poll on the same already-seen alert skips replan and resend."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    alert = Alert(
        id="alert-c-seen",
        header="C train delays",
        description="Expect delays on the C line.",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=2))],
        severity="WARNING",
        url=None,
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    # Pre-mark alert as seen
    store.mark_alert_seen(alert.id, today_events[0].id)

    notifier = SpyNotifier()
    planner = MockPlanner(plan)

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [alert],
        alerts_affecting_route_fn=lambda alerts, route, at_time: [alert]
        if alert in alerts
        else [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
    )

    # No replan (alert already seen)
    assert len(planner.calls) == 0
    # No messages sent
    assert len(notifier.sent) == 0


# ── Quiet hours suppresses prep but not leave ──────────────────────────────────

def test_poll_quiet_hours_suppresses_prep_not_leave(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """During quiet hours, prep pings are suppressed but leave pings fire."""
    # Use a fixed "now" that falls inside overnight quiet hours (22:00–07:00)
    now = now_nyc().replace(hour=23, minute=30, second=0, microsecond=0)
    quiet_start = now.replace(hour=22, minute=0, second=0, microsecond=0).timetz()
    quiet_end = now.replace(hour=7, minute=0, second=0, microsecond=0).timetz()
    minimal_config.scheduling.quiet_hours_start = quiet_start
    minimal_config.scheduling.quiet_hours_end = quiet_end

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    # Both pings are already due
    leave_ping = PingEntry(
        id="ping-leave-qh",
        event_id=today_events[0].id,
        kind="leave",
        fire_at=now - timedelta(minutes=5),
        fired=False,
        message="🚶 *Leave now*",
    )
    prep_ping = PingEntry(
        id="ping-prep-qh",
        event_id=today_events[0].id,
        kind="prep",
        fire_at=now - timedelta(minutes=25),
        fired=False,
        message="⏰ *Start prep*",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(leave_ping)
    store.schedule_ping(prep_ping)

    notifier = SpyNotifier()

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
    )

    # Leave ping fires
    assert "🚶 *Leave now*" in notifier.sent
    # Prep ping is suppressed during quiet hours
    assert "⏰ *Start prep*" not in notifier.sent


# ── No duplicate sends for seen alerts ───────────────────────────────────────

def test_poll_no_duplicate_service_updates_for_seen_alert(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
    wide_today_window: None,
) -> None:
    """A seen alert does not cause a second service update when re-encountered."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    alert = Alert(
        id="alert-c-dup",
        header="C train delays",
        description="Expect delays on the C line.",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=2))],
        severity="WARNING",
        url=None,
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)

    notifier = SpyNotifier()

    # ── First poll: alert is new → replan (with a different route) → service update
    # Build a meaningfully different new plan (later leave_at triggers threshold)
    new_leave_at = leave_at + timedelta(minutes=15)
    new_prep_at = new_leave_at - timedelta(minutes=20)
    new_route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St (delayed)",
                depart_at=new_leave_at - timedelta(minutes=45),
                arrive_at=new_leave_at,
                duration_seconds=2700,
                summary="C train to Fulton St (delayed)",
            ),
        ],
        depart_at=new_leave_at - timedelta(minutes=45),
        arrive_at=new_leave_at,
        total_duration_seconds=2700,
        transfers=0,
    )
    replanned_plan = Plan(
        event=today_events[0],
        route=new_route,
        leave_at=new_leave_at,
        prep_at=new_prep_at,
    )
    planner = MockPlanner(replanned_plan)

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [alert],
        alerts_affecting_route_fn=lambda alerts, route, at_time: [alert]
        if alert in alerts
        else [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
    )

    first_send_count = len(notifier.sent)
    assert first_send_count >= 1, f"Expected at least 1 send, got {first_send_count}: {notifier.sent}"

    # ── Second poll: same alert now seen → no replan → no new send
    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [alert],
        alerts_affecting_route_fn=lambda alerts, route, at_time: [alert]
        if alert in alerts
        else [],
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(replanned_plan),
        now_fn=lambda: now + timedelta(minutes=1),
    )

    # No additional messages
    assert len(notifier.sent) == first_send_count


def test_poll_uses_select_alerts_fn_for_smarter_filtering(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
    wide_today_window: None,
) -> None:
    now = now_nyc()

    leave_at = (now + timedelta(hours=2)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    original_plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    # Build changed plan so a selected alert causes a service update.
    new_leave_at = leave_at + timedelta(minutes=20)
    new_route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Delayed",
                depart_at=new_leave_at - timedelta(minutes=45),
                arrive_at=new_leave_at,
                duration_seconds=2700,
                summary="C delayed",
            ),
        ],
        depart_at=new_leave_at - timedelta(minutes=45),
        arrive_at=new_leave_at,
        total_duration_seconds=2700,
        transfers=0,
    )
    replanned = Plan(
        event=today_events[0],
        route=new_route,
        leave_at=new_leave_at,
        prep_at=new_leave_at - timedelta(minutes=20),
    )

    actionable = Alert(
        id="a-action",
        header="C train delays",
        description="Serious delays",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=1))],
        severity="WARNING",
        url=None,
    )
    noise = Alert(
        id="a-noise",
        header="Elevator unavailable",
        description="Use stairs",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=1))],
        severity="INFO",
        url=None,
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(original_plan)

    planner = MockPlanner(replanned)
    notifier = SpyNotifier()

    def select_only_actionable(alerts: list[Alert], route: Route, at_time: datetime, llm: Any = None) -> list[Alert]:
        return [a for a in alerts if a.id == "a-action"]

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [actionable, noise],
        select_alerts_fn=select_only_actionable,
        # The notifier param is Optional[TelegramNotifier]. SpyNotifier is duck-type
        # compatible (same send signature) but not a subclass, so we cast it
        # to TelegramNotifier to satisfy mypy while preserving runtime behavior.
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
    )

    assert len(planner.calls) == 1
    assert store.is_alert_seen("a-action", today_events[0].id)
    assert not store.is_alert_seen("a-noise", today_events[0].id)


# ── Home Assistant pull + location-driven replan ───────────────────────────────


def _ha_enabled(cfg: Config) -> Config:
    from commutecompass.config import HomeAssistantConfig

    return cfg.model_copy(
        update={
            "home_assistant": HomeAssistantConfig(
                enabled=True,
                base_url="http://ha",
                entity_id="device_tracker.iphone",
                home_zone="home",
                max_age_minutes=30,
                replan_window_minutes=30,
            ),
            "home_assistant_token": "tok",
        }
    )


def test_poll_calls_ha_fetch_when_enabled_and_skips_when_disabled(
    minimal_config: Config,
) -> None:
    """ha_fetch_fn is invoked once when enabled; not called when disabled."""
    from commutecompass.models import CurrentLocation

    now = now_nyc()
    cfg_on = _ha_enabled(minimal_config)
    store = Store(cfg_on.paths.db_path)
    store.init_schema()

    fetch_calls: list[tuple[str, str, str]] = []

    def _fetch(base_url: str, entity_id: str, token: str, **_kw: Any) -> CurrentLocation:
        fetch_calls.append((base_url, entity_id, token))
        return CurrentLocation(
            lat=40.7128, lon=-74.006, zone="not_home", captured_at=now
        )

    poll_run(
        cfg_on,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, SpyNotifier()),
        plan_event_fn=MockPlanner(plan=Plan(event=Event(
            id="evt-x", calendar_id="c", calendar_name="C",
            title="t", start=now, end=now,
        ))),
        now_fn=lambda: now,
        ha_fetch_fn=_fetch,
    )

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == ("http://ha", "device_tracker.iphone", "tok")
    # Latest location persisted
    cl = store.get_current_location()
    assert cl is not None and cl.lat == 40.7128

    # When disabled, no fetch should happen
    fetch_calls.clear()
    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, SpyNotifier()),
        plan_event_fn=MockPlanner(plan=Plan(event=Event(
            id="evt-x", calendar_id="c", calendar_name="C",
            title="t", start=now, end=now,
        ))),
        now_fn=lambda: now,
        ha_fetch_fn=_fetch,
    )
    assert fetch_calls == []


def test_poll_location_replan_sends_update_when_route_shifts(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
    wide_today_window: None,
) -> None:
    """Within replan window, a shifted leave_at triggers a location update."""
    cfg = _ha_enabled(minimal_config)
    now = now_nyc()

    # leave_at is 10 min from now → inside the 30-min replan window
    old_leave = now + timedelta(minutes=10)
    old_prep = old_leave - timedelta(minutes=20)
    old_plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=old_leave,
        prep_at=old_prep,
    )

    store = Store(cfg.paths.db_path)
    store.init_schema()
    store.upsert_plan(old_plan)

    # New plan has leave_at 15 min later → significant shift
    new_leave = old_leave + timedelta(minutes=15)
    new_prep = new_leave - timedelta(minutes=20)
    new_route = Route(
        legs=[
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=new_leave, arrive_at=new_leave + timedelta(minutes=10),
                duration_seconds=600, summary="Walk",
            ),
        ],
        depart_at=new_leave,
        arrive_at=new_leave + timedelta(minutes=10),
        total_duration_seconds=600,
        transfers=0,
    )
    new_plan = Plan(event=today_events[0], route=new_route, leave_at=new_leave, prep_at=new_prep)

    notifier = SpyNotifier()
    planner = MockPlanner(plan=new_plan)

    poll_run(
        cfg,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
        ha_fetch_fn=lambda *a, **kw: None,
    )

    # Update was sent
    assert any("Location update" in m for m in notifier.sent)
    # Plan was upserted (new leave_at persisted)
    saved = store.get_plan(today_events[0].id)
    assert saved is not None and saved.leave_at == new_leave


def test_poll_ha_alarm_fires_for_prep_and_leave_only(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """When ha_alarm_notifier is supplied, it fires alongside the primary notifier
    for prep/leave pings only — not for digest or service_update."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    prep_at = leave_at - timedelta(minutes=20)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=prep_at,
    )

    leave_ping = PingEntry(
        id="ping-leave",
        event_id=today_events[0].id,
        kind="leave",
        fire_at=now - timedelta(minutes=1),
        fired=False,
        message="leave now",
    )
    prep_ping = PingEntry(
        id="ping-prep",
        event_id=today_events[0].id,
        kind="prep",
        fire_at=now - timedelta(minutes=21),
        fired=False,
        message="get ready",
    )
    digest_ping = PingEntry(
        id="ping-digest",
        event_id=today_events[0].id,
        kind="digest",
        fire_at=now - timedelta(minutes=120),
        fired=False,
        message="digest body",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(leave_ping)
    store.schedule_ping(prep_ping)
    store.schedule_ping(digest_ping)

    notifier = SpyNotifier()
    alarm = SpyNotifier()

    # Configure alarm kinds via config — default is ["prep", "leave"]
    minimal_config.home_assistant.alarm.enabled = True
    minimal_config.home_assistant.alarm.service = "script.commute_alarm"

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        ha_alarm_notifier=cast(TelegramNotifier, alarm),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
    )

    # Primary notifier saw all three pings
    assert set(notifier.sent) == {"leave now", "get ready", "digest body"}
    # Alarm notifier saw only prep + leave — digest is NOT alarm-worthy
    assert set(alarm.sent) == {"leave now", "get ready"}


def test_poll_ha_alarm_failure_does_not_unfire_primary(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """If the HA alarm POST fails, the primary ping stays marked-fired and is not
    re-attempted on the next poll."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=leave_at - timedelta(minutes=20),
    )
    leave_ping = PingEntry(
        id="ping-leave",
        event_id=today_events[0].id,
        kind="leave",
        fire_at=now - timedelta(minutes=1),
        fired=False,
        message="leave now",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(leave_ping)

    notifier = SpyNotifier()
    failing_alarm = SpyNotifier(return_value=False)

    minimal_config.home_assistant.alarm.enabled = True
    minimal_config.home_assistant.alarm.service = "script.commute_alarm"

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        ha_alarm_notifier=cast(TelegramNotifier, failing_alarm),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
    )

    # Primary fired once, alarm attempted once
    assert notifier.sent == ["leave now"]
    assert failing_alarm.sent == ["leave now"]

    # Second poll: ping was marked fired despite alarm failure → no resend
    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        ha_alarm_notifier=cast(TelegramNotifier, failing_alarm),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now + timedelta(minutes=1),
    )
    assert notifier.sent == ["leave now"]
    assert failing_alarm.sent == ["leave now"]


def test_poll_ha_alarm_not_fired_when_disabled(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """When alarm.enabled = False (the default), no HA alarm fires even if a
    notifier instance is somehow available."""
    now = now_nyc()

    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=leave_at - timedelta(minutes=20),
    )
    leave_ping = PingEntry(
        id="ping-leave",
        event_id=today_events[0].id,
        kind="leave",
        fire_at=now - timedelta(minutes=1),
        fired=False,
        message="leave now",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(leave_ping)

    notifier = SpyNotifier()

    # Don't enable alarm; ha_alarm_notifier defaults to None and build_ha_alarm_notifier
    # returns None because alarm.enabled is False.
    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
    )

    assert notifier.sent == ["leave now"]


def test_poll_location_replan_outside_window_is_noop(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Plans whose leave_at is far in the future are not replanned by phase 5."""
    cfg = _ha_enabled(minimal_config)
    now = now_nyc()

    # leave_at 2 hours from now → outside default 30 min window
    far_leave = now + timedelta(hours=2)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=far_leave,
        prep_at=far_leave - timedelta(minutes=20),
    )
    store = Store(cfg.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)

    notifier = SpyNotifier()
    planner = MockPlanner(plan=plan)

    poll_run(
        cfg,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
        ha_fetch_fn=lambda *a, **kw: None,
    )

    assert planner.calls == []  # no replan attempted
    assert notifier.sent == []


def test_poll_location_replan_skips_leg_only_change(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """A sub-threshold leave_at shift with only leg-set differences must not notify.

    Reproduces the spam pattern where the planner flaps between two
    near-equivalent options (Mixed transit vs Subway-C-only) at the same
    leave time. Phase 5 should ignore these.
    """
    cfg = _ha_enabled(minimal_config)
    now = now_nyc()

    old_leave = now + timedelta(minutes=10)
    old_prep = old_leave - timedelta(minutes=20)
    old_plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=old_leave,
        prep_at=old_prep,
    )

    store = Store(cfg.paths.db_path)
    store.init_schema()
    store.upsert_plan(old_plan)

    # New plan: leave_at drifts by 1 minute, legs flip to a different system/line.
    new_leave = old_leave + timedelta(minutes=1)
    new_prep = new_leave - timedelta(minutes=20)
    new_route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="A", headsign="Far Rockaway",
                depart_at=new_leave, arrive_at=new_leave + timedelta(minutes=30),
                duration_seconds=1800, summary="A train",
            ),
        ],
        depart_at=new_leave,
        arrive_at=new_leave + timedelta(minutes=30),
        total_duration_seconds=1800,
        transfers=0,
    )
    new_plan = Plan(event=today_events[0], route=new_route, leave_at=new_leave, prep_at=new_prep)

    notifier = SpyNotifier()
    planner = MockPlanner(plan=new_plan)

    poll_run(
        cfg,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
        ha_fetch_fn=lambda *a, **kw: None,
    )

    # No location update sent, and the stored plan is unchanged.
    assert not any("Location update" in m for m in notifier.sent)
    saved = store.get_plan(today_events[0].id)
    assert saved is not None and saved.leave_at == old_leave


def test_poll_mta_fetch_cached_within_ttl(
    minimal_config: Config,
) -> None:
    """Two poll runs within the cache TTL must hit the MTA feed only once.

    Exercises the production fetch path (no fetch_alerts_fn injection) by
    patching commutecompass.mta.fetch_alerts. The module-level cache is reset
    before and after so other tests are unaffected.
    """
    from commutecompass.jobs import poll as poll_mod

    cfg = _ha_enabled(minimal_config)
    store = Store(cfg.paths.db_path)
    store.init_schema()

    now = now_nyc()
    later = now + timedelta(seconds=60)  # well under the 180s TTL

    fetch_calls: list[dict[str, Any]] = []

    def _counting_fetch(**kw: Any) -> list[Any]:
        fetch_calls.append(kw)
        return []

    poll_mod._alerts_cache = None
    try:
        with patch("commutecompass.mta.fetch_alerts", _counting_fetch):
            poll_run(
                cfg,
                store=store,
                notifier=cast(TelegramNotifier, SpyNotifier()),
                plan_event_fn=MockPlanner(plan=Plan(event=Event(
                    id="evt-x", calendar_id="c", calendar_name="C",
                    title="t", start=now, end=now,
                ))),
                now_fn=lambda: now,
                ha_fetch_fn=lambda *a, **kw: None,
            )
            poll_run(
                cfg,
                store=store,
                notifier=cast(TelegramNotifier, SpyNotifier()),
                plan_event_fn=MockPlanner(plan=Plan(event=Event(
                    id="evt-x", calendar_id="c", calendar_name="C",
                    title="t", start=later, end=later,
                ))),
                now_fn=lambda: later,
                ha_fetch_fn=lambda *a, **kw: None,
            )

        assert len(fetch_calls) == 1, (
            f"expected 1 MTA fetch within TTL, got {len(fetch_calls)}"
        )
    finally:
        poll_mod._alerts_cache = None


# ── Daily alert dedup across events ───────────────────────────────────────────


def test_poll_dedups_same_alert_across_multiple_events(
    minimal_config: Config,
    sample_route: Route,
    wide_today_window: None,
) -> None:
    """One alert affecting two events on the same day yields exactly one send.

    Daily dedup spares the user from getting "C train delayed" three times
    just because three of today's events use the C line.  The poll still
    silently re-plans each affected event; only the *notification* is
    suppressed for the second one onward.
    """
    now = now_nyc()
    base_start = now + timedelta(hours=3)

    event_a = Event(
        id="evt-a",
        calendar_id="cal-001",
        calendar_name="Test",
        title="Event A",
        start=base_start,
        end=base_start + timedelta(hours=1),
        location_raw="200 Example St, New York, NY",
    )
    event_b = Event(
        id="evt-b",
        calendar_id="cal-001",
        calendar_name="Test",
        title="Event B",
        start=base_start + timedelta(hours=2),
        end=base_start + timedelta(hours=3),
        location_raw="200 Example St, New York, NY",
    )

    plan_a = Plan(
        event=event_a,
        route=sample_route,
        leave_at=event_a.start - timedelta(minutes=45),
        prep_at=event_a.start - timedelta(minutes=65),
    )
    plan_b = Plan(
        event=event_b,
        route=sample_route,
        leave_at=event_b.start - timedelta(minutes=45),
        prep_at=event_b.start - timedelta(minutes=65),
    )

    alert = Alert(
        id="alert-shared",
        header="C train delays",
        description="Big delays on the C.",
        affected_routes={"C"},
        affected_systems={"MTA Subway"},
        active_periods=[(now - timedelta(hours=1), now + timedelta(hours=5))],
        severity="WARNING",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan_a)
    store.upsert_plan(plan_b)

    # New plan that triggers a "route change" so service_update fires for the
    # first event, then would fire for the second if dedup weren't in place.
    def _replanned(event: Event) -> Plan:
        new_leave = event.start - timedelta(minutes=20)
        new_route = Route(
            legs=[
                TransitLeg(
                    mode="TRANSIT",
                    system="MTA Subway",
                    line="C",
                    headsign="Fulton St (delayed)",
                    depart_at=new_leave - timedelta(minutes=20),
                    arrive_at=new_leave,
                    duration_seconds=1200,
                    summary="Delayed C",
                ),
            ],
            depart_at=new_leave - timedelta(minutes=20),
            arrive_at=new_leave,
            total_duration_seconds=1200,
            transfers=0,
        )
        return Plan(
            event=event,
            route=new_route,
            leave_at=new_leave,
            prep_at=new_leave - timedelta(minutes=20),
        )

    def planner(event: Event, **_kw: object) -> Plan:
        return _replanned(event)

    notifier = SpyNotifier()

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [alert],
        alerts_affecting_route_fn=lambda alerts, route, at_time: [alert]
        if alert in alerts
        else [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=planner,
        now_fn=lambda: now,
        ha_fetch_fn=lambda *a, **kw: None,
    )

    # Exactly one service_update message reaches the user, even though the
    # alert affected both today's events.  Both events are still replanned;
    # we just don't spam the second notification.
    service_msgs = [m for m in notifier.sent if "Service Change" in m or "service" in m.lower()]
    assert len(service_msgs) == 1, f"Expected 1 service_update, got {len(service_msgs)}: {notifier.sent}"


# ── Atomic claim semantics ────────────────────────────────────────────────────


@pytest.mark.parametrize("mutation", ["refresh", "replan"])
@pytest.mark.parametrize("send_ok", [True, False])
def test_poll_dispatch_claims_current_payload_after_concurrent_update(
    minimal_config: Config, store: Store, mutation: str, send_ok: bool,
) -> None:
    from commutecompass.format import format_leave_ping

    now = now_nyc().replace(hour=8, minute=0, second=0, microsecond=0)
    plan = refresh_fixture(store, now, leave_minutes=0)
    store.claim_ping("prep", now)  # Only the leave alarm is pending.
    store.claim_ping("leave", now)
    store.release_ping("leave")  # Preserve an existing failed-send attempt.
    other = Store(minimal_config.paths.db_path)
    pending_pings = store.pending_pings

    def read_then_update(before: datetime) -> list[PingEntry]:
        snapshot = pending_pings(before)
        assert len(snapshot) == 1 and snapshot[0].id == "leave"
        if mutation == "refresh":
            assert other.increase_realtime_buffer(plan, 10, "Q late", now)
        else:
            assert plan.leave_at is not None and plan.prep_at is not None
            changed = plan.model_copy(update={
                "leave_at": plan.leave_at - timedelta(minutes=10),
                "prep_at": plan.prep_at - timedelta(minutes=10),
            })
            assert other.reconcile_poll_replan(plan, changed, now)
        current = other.get_pending_ping(plan.event.id, "leave")
        assert current is not None and current.id == snapshot[0].id
        if mutation == "refresh":
            assert current.fire_at == snapshot[0].fire_at  # No timestamp CAS can detect this.
        else:
            assert current.fire_at <= snapshot[0].fire_at
        assert current.message != snapshot[0].message
        return snapshot

    notifier = SpyNotifier(return_value=send_ok)
    with patch.object(store, "pending_pings", side_effect=read_then_update):
        _poll_at(minimal_config, store, plan, notifier, now)
    saved = other.get_plan(plan.event.id)
    assert saved is not None and saved.leave_at == now - timedelta(minutes=10)
    message = format_leave_ping(saved)
    assert notifier.sent == [message]
    with other._connect() as conn:
        row = conn.execute(
            "SELECT id, fired, send_attempts, fired_at FROM pings WHERE id = 'leave'",
        ).fetchone()
    assert row == ("leave", int(send_ok), 1 if send_ok else 2,
                   now.isoformat() if send_ok else None)
    notifier._return_value = True
    _poll_at(minimal_config, store, saved, notifier, now)
    assert notifier.sent == [message] * (1 if send_ok else 2)
    assert other.claim_ping_entry("leave", now) is None
    _poll_at(minimal_config, store, saved, notifier, now)
    assert notifier.sent == [message] * (1 if send_ok else 2)


def test_poll_does_not_claim_same_id_postponed_after_due_snapshot(
    minimal_config: Config, store: Store,
) -> None:
    from commutecompass.format import format_leave_ping

    now = now_nyc().replace(hour=8, minute=0, second=0, microsecond=0)
    plan = refresh_fixture(store, now, leave_minutes=0)
    store.claim_ping("prep", now)
    store.claim_ping("leave", now)
    store.release_ping("leave")
    other = Store(minimal_config.paths.db_path)
    pending_pings = store.pending_pings
    assert plan.leave_at is not None and plan.prep_at is not None
    changed = plan.model_copy(update={
        "leave_at": plan.leave_at + timedelta(minutes=10),
        "prep_at": plan.prep_at + timedelta(minutes=10),
    })

    def read_then_postpone(before: datetime) -> list[PingEntry]:
        snapshot = pending_pings(before)
        assert len(snapshot) == 1 and snapshot[0].id == "leave"
        # Captured before the old deadline, but committed after the candidate read.
        assert other.reconcile_poll_replan(plan, changed, now - timedelta(seconds=1))
        return snapshot

    notifier = SpyNotifier()
    with patch.object(store, "pending_pings", side_effect=read_then_postpone):
        _poll_at(minimal_config, store, plan, notifier, now)
    assert notifier.sent == []
    pending = other.pending_pings(now + timedelta(minutes=10))[0]
    assert pending is not None and pending.id == "leave"
    assert pending.fire_at == now + timedelta(minutes=10)
    assert not pending.fired and pending.fired_at is None and pending.send_attempts == 1
    _poll_at(minimal_config, store, changed, notifier, now + timedelta(minutes=9))
    assert notifier.sent == []
    _poll_at(minimal_config, store, changed, notifier, pending.fire_at)
    assert notifier.sent == [format_leave_ping(changed)]
    _poll_at(minimal_config, store, changed, notifier, pending.fire_at)
    assert notifier.sent == [format_leave_ping(changed)]
    with other._connect() as conn:
        row = conn.execute(
            "SELECT fired, fired_at, send_attempts FROM pings WHERE id = 'leave'",
        ).fetchone()
    assert row == (1, pending.fire_at.isoformat(), 1)


@pytest.mark.parametrize("field", ["kind", "event_id", "send_attempts", "fire_at"])
def test_poll_dispatch_uses_current_claim_metadata(
    minimal_config: Config, store: Store, field: str,
) -> None:
    from commutecompass.jobs.poll import _MAX_SEND_ATTEMPTS

    now = now_nyc().replace(hour=12, minute=0, second=0, microsecond=0)
    plan = refresh_fixture(store, now, leave_minutes=0)
    store.claim_ping("prep", now)
    other = Store(minimal_config.paths.db_path)
    pending_pings = store.pending_pings
    values: dict[str, object] = {
        "kind": "service_update", "event_id": "muted-current",
        "send_attempts": _MAX_SEND_ATTEMPTS - 1,
        "fire_at": (now - timedelta(hours=1)).isoformat(),
    }
    if field == "event_id":
        other.mute_event("muted-current")

    def read_then_update(before: datetime) -> list[PingEntry]:
        snapshot = pending_pings(before)
        with other._connect() as conn:
            conn.execute(f"UPDATE pings SET {field} = ? WHERE id = 'leave'", (values[field],))
        return snapshot

    notifier = SpyNotifier(return_value=False)
    with patch.object(store, "pending_pings", side_effect=read_then_update):
        _poll_at(minimal_config, store, plan, notifier, now)
    assert notifier.sent == ([] if field == "event_id" else ["leave"])
    assert other.pending_pings(now) == []  # Current mute/kind/cap/grace forbids retry.
    with other._connect() as conn:
        row = conn.execute("SELECT fired, send_attempts FROM pings WHERE id = 'leave'").fetchone()
    assert row == (1, _MAX_SEND_ATTEMPTS - 1 if field == "send_attempts" else 0)


class FlakyNotifier:
    """Notifier that fails its first ``fail_times`` sends, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times

    def send(self, text: str) -> bool:
        self.sent.append(text)
        ok = len(self.sent) > self._fail_times
        return ok


def _seed_due_leave_plan(
    config: Config,
    event: Event,
    route: Route,
    now: "datetime",
    *,
    fire_offset_minutes: int = 5,
    kind: str = "leave",
) -> Store:
    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    plan = Plan(
        event=event,
        route=route,
        leave_at=leave_at,
        prep_at=leave_at - timedelta(minutes=20),
    )
    store = Store(config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(
        PingEntry(
            id="ping-flaky",
            event_id=event.id,
            kind=cast("Any", kind),
            fire_at=now - timedelta(minutes=fire_offset_minutes),
            fired=False,
            message="leave now",
        )
    )
    return store


def _poll_at(config: Config, store: Store, plan: Plan, notifier: object, at: "datetime") -> None:
    poll_run(
        config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: at,
        ha_fetch_fn=lambda *a, **kw: None,
    )


def test_poll_releases_leave_ping_for_retry_when_send_fails(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """A leave ping whose send fails is handed back so the next poll retries.

    Losing the one notification that matters is worse than a second attempt: an
    actionable ping within its grace window is re-fired on the following poll.
    """
    now = now_nyc()
    store = _seed_due_leave_plan(minimal_config, today_events[0], sample_route, now)
    plan = store.get_plan(today_events[0].id)
    assert plan is not None

    failing = SpyNotifier(return_value=False)
    _poll_at(minimal_config, store, plan, failing, now)
    assert failing.sent == ["leave now"]
    # Released back to the pending pool with the attempt recorded.
    pending = store.pending_pings(before=now + timedelta(minutes=1))
    assert [p.id for p in pending] == ["ping-flaky"]
    assert pending[0].send_attempts == 1

    # A minute later (still inside the grace window) it retries.
    _poll_at(minimal_config, store, plan, failing, now + timedelta(minutes=1))
    assert failing.sent == ["leave now", "leave now"]


def test_poll_stops_retrying_once_send_succeeds(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    now = now_nyc()
    store = _seed_due_leave_plan(minimal_config, today_events[0], sample_route, now)
    plan = store.get_plan(today_events[0].id)
    assert plan is not None

    flaky = FlakyNotifier(fail_times=1)  # first send fails, second succeeds
    _poll_at(minimal_config, store, plan, flaky, now)
    _poll_at(minimal_config, store, plan, flaky, now + timedelta(minutes=1))
    assert flaky.sent == ["leave now", "leave now"]

    # Now fired for good — a third poll does not send again.
    _poll_at(minimal_config, store, plan, flaky, now + timedelta(minutes=2))
    assert flaky.sent == ["leave now", "leave now"]
    assert store.pending_pings(before=now + timedelta(minutes=5)) == []


def test_poll_gives_up_after_attempt_cap(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """A persistently-broken notifier must not retry forever (no storm)."""
    now = now_nyc()
    store = _seed_due_leave_plan(minimal_config, today_events[0], sample_route, now)
    plan = store.get_plan(today_events[0].id)
    assert plan is not None

    failing = SpyNotifier(return_value=False)
    # Poll once per minute; after _MAX_SEND_ATTEMPTS the row is abandoned.
    for i in range(8):
        _poll_at(minimal_config, store, plan, failing, now + timedelta(minutes=i))

    from commutecompass.jobs.poll import _MAX_SEND_ATTEMPTS

    assert len(failing.sent) == _MAX_SEND_ATTEMPTS
    assert store.pending_pings(before=now + timedelta(minutes=10)) == []


def test_poll_records_heartbeat(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Every poll run records a 'poll' heartbeat for the dead-man's-switch."""
    now = now_nyc()
    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    assert store.get_job_heartbeat("poll") is None

    _poll_at(minimal_config, store, Plan(event=today_events[0]), SpyNotifier(), now)
    assert store.get_job_heartbeat("poll") == now


def test_poll_does_not_retry_stale_leave_ping(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """Outside the grace window a failed leave send is abandoned, not re-fired."""
    now = now_nyc()
    # fire_at is well past the grace window already.
    store = _seed_due_leave_plan(
        minimal_config, today_events[0], sample_route, now, fire_offset_minutes=30
    )
    plan = store.get_plan(today_events[0].id)
    assert plan is not None

    failing = SpyNotifier(return_value=False)
    _poll_at(minimal_config, store, plan, failing, now)
    assert failing.sent == ["leave now"]
    # Stale: consumed, not released.
    assert store.pending_pings(before=now + timedelta(minutes=1)) == []


def test_poll_quiet_hours_leaves_unclaimed_for_later(
    minimal_config: Config,
    today_events: list[Event],
    sample_route: Route,
) -> None:
    """A prep ping suppressed by quiet hours must remain claimable later.

    Quiet hours suppression filters BEFORE the atomic claim — otherwise a
    suppressed ping would be silently consumed and never delivered.
    """
    from datetime import time as _time

    now = now_nyc()
    leave_at = (now + timedelta(hours=3)) - timedelta(minutes=45)
    plan = Plan(
        event=today_events[0],
        route=sample_route,
        leave_at=leave_at,
        prep_at=leave_at - timedelta(minutes=20),
    )
    prep_ping = PingEntry(
        id="ping-prep-quiet",
        event_id=today_events[0].id,
        kind="prep",
        fire_at=now - timedelta(minutes=5),
        fired=False,
        message="start prep",
    )

    store = Store(minimal_config.paths.db_path)
    store.init_schema()
    store.upsert_plan(plan)
    store.schedule_ping(prep_ping)

    # Quiet-hours window covering 'now' in NYC local.
    nyc_now = now.astimezone(NYC_TZ)
    minimal_config.scheduling.quiet_hours_start = _time(
        (nyc_now.hour - 1) % 24, 0
    )
    minimal_config.scheduling.quiet_hours_end = _time(
        (nyc_now.hour + 1) % 24, 59
    )

    notifier = SpyNotifier()

    poll_run(
        minimal_config,
        store=store,
        fetch_alerts_fn=lambda **kw: [],
        alerts_affecting_route_fn=lambda *a, **kw: [],
        notifier=cast(TelegramNotifier, notifier),
        plan_event_fn=MockPlanner(plan),
        now_fn=lambda: now,
        ha_fetch_fn=lambda *a, **kw: None,
    )

    # Suppressed: not sent and not claimed
    assert notifier.sent == []
    pending = store.pending_pings(before=now + timedelta(hours=1))
    assert any(p.id == "ping-prep-quiet" for p in pending), (
        "Quiet-hours suppression must NOT consume the ping; it should remain "
        "pending so a later poll outside the quiet window can claim it."
    )
