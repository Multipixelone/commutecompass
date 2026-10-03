"""Poll loop job."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Callable, Optional

from commutecompass.config import Config
from commutecompass.models import Alert, CurrentLocation, Plan, PingEntry, ZoneInfo
from commutecompass.timeutil import is_within_quiet_hours, now_nyc

if TYPE_CHECKING:
    from datetime import datetime
    from commutecompass.store import Store
    from commutecompass.notify import Notifier
    from commutecompass.llm import OpencodeGoClient

logger = logging.getLogger(__name__)

# Minimum time difference to trigger a service update (in seconds)
_REPLAN_THRESHOLD_SECONDS = 5 * 60

# Independent of HA: refresh only active plans departing within an hour (or
# overdue by at most the actionable send grace). No Directions replan here.
_REALTIME_REFRESH_HORIZON_MINUTES = 60

# How long to reuse a fetched MTA alert set inside the poll loop (in seconds).
# Only applied when the caller did not inject a fetch_alerts_fn (tests bypass).
_MTA_CACHE_TTL_SECONDS = 180

# Re-fire policy for actionable pings whose send failed transiently.  The claim
# already consumed the row; on failure we hand it back (release_ping) so the
# next poll re-attempts — but only for these kinds, only within the grace window
# of their scheduled time (a stale alarm is worse than none), and only up to the
# attempt cap so a persistently-broken notifier can never storm.
_RETRYABLE_PING_KINDS = frozenset({"prep", "leave"})
_MAX_SEND_ATTEMPTS = 5
_SEND_RETRY_GRACE_SECONDS = 15 * 60

# Module-level memo: (captured_at, (subway_url, lirr_url, bus_url), alerts).
_alerts_cache: "tuple[datetime, tuple[str, str, str], list[Alert]] | None" = None


def run(
    config: Config,
    *,
    store: Optional[Store] = None,
    fetch_alerts_fn: Optional[Callable[..., list[Alert]]] = None,
    alerts_affecting_route_fn: Optional[Callable[..., list[Alert]]] = None,
    select_alerts_fn: Optional[Callable[..., list[Alert]]] = None,
    notifier: Optional["Notifier"] = None,
    ha_alarm_notifier: Optional["Notifier"] = None,
    plan_event_fn: Optional[Callable[..., Plan]] = None,
    now_fn: Optional[Callable[[], "datetime"]] = None,
    ha_fetch_fn: Optional[Callable[..., Optional[CurrentLocation]]] = None,
    ha_zones_fn: Optional[Callable[..., dict[str, ZoneInfo]]] = None,
) -> None:
    """Run the poll loop job.

    Sequence (§6.15):
    1. Honor quiet hours — suppress prep/service_update pings (leave always fires)
    2. Refresh near-departure realtime padding, then atomically claim due pings
    3. Fetch fresh MTA alerts
    4. For each new alert affecting a today-plan:
       a. Re-plan the event
        b. If route changed significantly: atomically reconcile the plan and
           actionable pings, then send service_update (discard stale results)
       c. Mark alert seen for that event

    All external dependencies are injectable for testability.

    Args:
        config: Application configuration.
        store: SQLite store (default: real Store from config).
        fetch_alerts_fn: Alert fetcher function (default: real fetch_alerts).
        alerts_affecting_route_fn: Alert matcher (default: real alerts_affecting_route).
        notifier: Telegram notifier (default: real TelegramNotifier).
        plan_event_fn: Event planner (default: real plan_event).
        now_fn: Time provider (default: real now_nyc).
    """
    # Resolve deps
    from commutecompass.store import Store
    from commutecompass.mta import alerts_affecting_route as _affecting
    from commutecompass.mta import fetch_alerts as _fetch
    from commutecompass.mta import select_actionable_alerts as _select_actionable
    from commutecompass.notify import build_ha_alarm_notifier, build_notifier
    from commutecompass.planner import plan_event as _plan_event
    from commutecompass.llm import OpencodeGoClient

    _store: Store = store or Store(config.paths.db_path)
    _use_alerts_cache = fetch_alerts_fn is None
    _fetch_alerts: Callable[..., list[Alert]] = fetch_alerts_fn or _fetch
    _alerts_affecting: Callable[..., list[Alert]] = alerts_affecting_route_fn or _affecting
    _select_alerts: Callable[..., list[Alert]]
    if select_alerts_fn is not None:
        _select_alerts = select_alerts_fn
    elif alerts_affecting_route_fn is not None:
        # Backward-compatible test injection path.
        _select_alerts = lambda alerts, route, at_time, llm=None: _alerts_affecting(  # noqa: E731
            alerts, route, at_time
        )
    else:
        _select_alerts = _select_actionable
    _notifier: Notifier = notifier or build_notifier(config)
    # Additive alarm channel — optional; None when not configured.  Caller may
    # also inject one (tests do).  We do not fall back to build_ha_alarm_notifier
    # when ha_alarm_notifier is explicitly None *and* the test path supplied
    # other overrides, so a None test value stays None.
    _ha_alarm: Optional[Notifier] = (
        ha_alarm_notifier if ha_alarm_notifier is not None else build_ha_alarm_notifier(config)
    )
    _ha_alarm_kinds: set[str] = set(config.home_assistant.alarm.kinds)
    _plan_event_fn: Callable[..., Plan] = plan_event_fn or _plan_event
    _now_fn: Callable[[], "datetime"] = now_fn or now_nyc
    if ha_fetch_fn is None:
        from commutecompass.ha_client import fetch_location as _ha_fetch
        _ha_fetch_fn: Callable[..., Optional[CurrentLocation]] = _ha_fetch
    else:
        _ha_fetch_fn = ha_fetch_fn
    if ha_zones_fn is None:
        from commutecompass.ha_client import fetch_zones as _ha_fetch_zones
        _ha_zones_fn: Callable[..., dict[str, ZoneInfo]] = _ha_fetch_zones
    else:
        _ha_zones_fn = ha_zones_fn
    llm_client: OpencodeGoClient | None = None
    if select_alerts_fn is None and alerts_affecting_route_fn is None:
        llm_client = OpencodeGoClient(
            endpoint=config.opencode_go.endpoint,
            token=config.opencode_go_token,
            model=config.opencode_go.model,
        )

    # ── Phase 0: refresh current location & zones from Home Assistant ─────────
    ha_zones: dict[str, ZoneInfo] = {}
    if config.home_assistant.enabled:
        try:
            loc = _ha_fetch_fn(
                config.home_assistant.base_url,
                config.home_assistant.entity_id,
                config.home_assistant_token,
                min_accuracy_m=float(config.home_assistant.min_gps_accuracy_meters),
            )
        except Exception as exc:
            logger.warning("HA fetch raised: %s", exc)
            loc = None
        if loc is not None:
            _store.upsert_current_location(loc)
            logger.debug(
                "ha_pull: ok lat=%.5f lon=%.5f zone=%s acc=%s",
                loc.lat,
                loc.lon,
                loc.zone,
                loc.accuracy_m,
            )

        try:
            ha_zones = _ha_zones_fn(
                config.home_assistant.base_url,
                config.home_assistant_token,
            )
        except Exception as exc:
            logger.warning("HA fetch_zones raised: %s", exc)
            ha_zones = {}

    # ── Phase 1: quiet-hours check ─────────────────────────────────────────────
    now = _now_fn()
    quiet_start = config.scheduling.quiet_hours_start
    quiet_end = config.scheduling.quiet_hours_end
    def quiet_at(at: "datetime") -> bool:
        return (
            quiet_start is not None
            and quiet_end is not None
            and is_within_quiet_hours(at, quiet_start, quiet_end)
        )

    # Refresh persisted routes BEFORE dispatch: newly urgent alarms fire this
    # cycle. Unknown/unmatched/smaller observations retain existing padding.
    if config.realtime.enabled:
        from commutecompass.realtime import realtime_delay

        for plan in _store.realtime_refresh_plans(
            now, horizon_minutes=_REALTIME_REFRESH_HORIZON_MINUTES,
            grace_seconds=_SEND_RETRY_GRACE_SECONDS,
        ):
            if plan.route is None or plan.error is not None:
                continue
            # The selection window is a prefetch snapshot; producer freshness
            # uses a live clock, independently of scheduled route departures.
            observation = realtime_delay(
                plan.route, now, config.realtime, clock=_now_fn,
            )
            now = _now_fn()
            if observation.status != "observed" or observation.minutes <= plan.realtime_buffer_minutes:
                continue
            try:
                _store.increase_realtime_buffer(plan, observation.minutes, observation.reason, now)
            except Exception as exc:
                logger.warning("Realtime refresh persistence failed for %s: %s", plan.event.id, exc)

    # ── Phase 2: fire due pings ───────────────────────────────────────────────
    # Atomic claim-then-send: every ping we try to send is first claimed in a
    # transaction returning its current payload, so concurrent refreshes cannot
    # leave us sending a stale pending snapshot. Failures use bounded retries.
    now = _now_fn()
    due_pings = _store.pending_pings(before=now)
    for candidate in due_pings:
        now = _now_fn()
        # Recheck the current deadline and quiet-hours kind atomically: a replan
        # may have postponed this same ID after the pending snapshot was read.
        ping = _store.claim_ping_entry(
            candidate.id, now, leave_only=quiet_at(now), due_only=True,
        )
        if ping is None:
            logger.debug("Ping %s already claimed or suppressed", candidate.id)
            continue

        # Honor `commutecompass mute <event>` / `mute --today`. The mute
        # ledger is forward-looking only; a row already fired stays fired.
        # We claim the ping anyway so the user's intent is recorded (it
        # doesn't re-fire on the next poll); the send is just skipped.
        if _store.is_muted(ping.event_id):
            logger.info(
                "Muted event %s — skipping %s ping %s",
                ping.event_id,
                ping.kind,
                ping.id,
            )
            continue

        sent_ok = _notifier.send(ping.message)
        now = _now_fn()
        if sent_ok:
            logger.info("Fired ping %s (%s)", ping.id, ping.kind)
        else:
            # The claim already set fired=1.  For actionable pings still within
            # their grace window and under the attempt cap, hand the row back so
            # the next poll retries; otherwise leave it fired (give up) so a
            # broken notifier can't storm or deliver a stale alarm.
            attempt = ping.send_attempts + 1
            within_grace = (
                now.astimezone(UTC) - ping.fire_at.astimezone(UTC)
            ).total_seconds() <= _SEND_RETRY_GRACE_SECONDS
            retryable = (
                ping.kind in _RETRYABLE_PING_KINDS
                and attempt < _MAX_SEND_ATTEMPTS
                and within_grace
            )
            if retryable and _store.release_ping(ping.id):
                logger.warning(
                    "Send failed for %s ping %s (attempt %d/%d) — released for retry",
                    ping.kind,
                    ping.id,
                    attempt,
                    _MAX_SEND_ATTEMPTS,
                )
            else:
                logger.warning(
                    "Send failed for claimed ping %s (%s) after %d attempt(s) — giving up",
                    ping.id,
                    ping.kind,
                    attempt,
                )

        # Additive HA alarm: fire AFTER the primary send attempt regardless of
        # its outcome (claim already consumed the row).  An HA outage cannot
        # un-fire the ping or cause repeat sends.
        if sent_ok and _ha_alarm is not None and ping.kind in _ha_alarm_kinds:
            if not _ha_alarm.send(ping.message):
                logger.warning(
                    "HA alarm send failed for ping %s (%s)", ping.id, ping.kind
                )

    # ── Phase 3: fetch alerts ─────────────────────────────────────────────────
    global _alerts_cache
    url_key = (
        config.mta.subway_alerts_url,
        config.mta.lirr_alerts_url,
        config.mta.bus_alerts_url,
    )
    alerts: list[Alert] | None = None
    now = _now_fn()
    if _use_alerts_cache and _alerts_cache is not None:
        cached_at, cached_urls, cached_alerts = _alerts_cache
        cache_age = (now.astimezone(UTC) - cached_at.astimezone(UTC)).total_seconds()
        if cached_urls == url_key and cache_age < _MTA_CACHE_TTL_SECONDS:
            alerts = cached_alerts
            logger.debug(
                "Reusing cached MTA alerts (%d, age %.0fs)",
                len(alerts),
                cache_age,
            )
    if alerts is None:
        alerts = _fetch_alerts(
            subway_url=url_key[0],
            lirr_url=url_key[1],
            bus_url=url_key[2],
        )
        now = _now_fn()
        logger.debug("Fetched %d MTA alerts", len(alerts))
        if _use_alerts_cache:
            _alerts_cache = (now, url_key, alerts)

    # ── Phase 4: process new affecting alerts ─────────────────────────────────
    today_plans = _store.today_plans()

    for plan in today_plans:
        now = _now_fn()
        if plan.event.start.astimezone(UTC) <= now.astimezone(UTC):
            continue
        if plan.route is None:
            continue
        if plan.leave_at is None:
            continue

        affecting = _select_alerts(
            alerts,
            plan.route,
            at_time=plan.leave_at,
            llm=llm_client,
        )

        for alert in affecting:
            if _store.is_alert_seen(alert.id, plan.event.id):
                logger.debug("Alert %s already seen for event %s", alert.id, plan.event.id)
                continue

            # New affecting alert — replan
            now = _now_fn()
            if plan.event.start.astimezone(UTC) <= now.astimezone(UTC):
                break
            try:
                new_plan = _plan_event_fn(
                    plan.event,
                    config=config,
                    venues=None,  # will be loaded by planner if needed
                    store=_store,
                    llm=None,  # not needed for replan; location already resolved
                    ha_zones=ha_zones,
                )
            except Exception as exc:
                logger.error("Replan failed for event %s: %s", plan.event.id, exc)
                _store.mark_alert_seen(alert.id, plan.event.id)
                continue

            new_plan = _retain_replan_safety(plan, new_plan)
            now = _now_fn()
            # Determine if the change warrants a service update
            route_changed = _route_significantly_different(plan, new_plan)

            if route_changed:
                if not _store.reconcile_poll_replan(plan, new_plan, now):
                    logger.debug("Discarding stale alert replan for %s", plan.event.id)
                    break  # Leave alert unseen for a fresh snapshot next cycle.
                # Daily dedup: if the same alert already triggered a
                # service_update for any of today's events, the user has been
                # told.  We still replan + reschedule pings (silent fix-up)
                # but suppress the duplicate notification.
                already_announced_today = _store.is_alert_seen_today(alert.id)

                if not already_announced_today:
                    from commutecompass.format import format_service_update

                    if new_plan.route is not None:
                        msg = format_service_update(plan, alert, new_plan.route)
                        if _notifier.send(msg):
                            logger.info("Sent service_update for event %s", plan.event.id)
                        else:
                            logger.warning(
                                "Failed to send service_update for event %s", plan.event.id
                            )
                else:
                    logger.debug(
                        "Alert %s already announced today — silently re-planning event %s",
                        alert.id,
                        plan.event.id,
                    )

                plan = new_plan
            else:
                # No significant change but still mark seen
                logger.debug(
                    "Alert %s affects event %s but route unchanged — marking seen",
                    alert.id,
                    plan.event.id,
                )

            _store.mark_alert_seen(alert.id, plan.event.id)

    # ── Phase 5: location-driven replan close to leave time ──────────────────
    now = _now_fn()
    if config.home_assistant.enabled and not quiet_at(now):
        from commutecompass.format import format_location_update

        window_seconds = config.home_assistant.replan_window_minutes * 60
        for plan in _store.today_plans():
            now = _now_fn()
            if plan.event.start.astimezone(UTC) <= now.astimezone(UTC):
                continue
            if plan.leave_at is None or plan.leave_at <= now:
                continue
            if (plan.leave_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds() > window_seconds:
                continue
            try:
                new_plan = _plan_event_fn(
                    plan.event,
                    config=config,
                    venues=None,
                    store=_store,
                    llm=None,
                    ha_zones=ha_zones,
                )
            except Exception as exc:
                logger.warning("Location replan failed for %s: %s", plan.event.id, exc)
                continue

            new_plan = _retain_replan_safety(plan, new_plan)
            now = _now_fn()
            if not _location_update_significant(plan, new_plan):
                continue

            if not _store.reconcile_poll_replan(plan, new_plan, now):
                logger.debug("Discarding stale location replan for %s", plan.event.id)
                continue
            msg = format_location_update(plan, new_plan)
            if _notifier.send(msg):
                logger.info("Sent location update for event %s", plan.event.id)
            else:
                logger.warning("Location update send failed for event %s", plan.event.id)

    # ── Phase 6: heartbeat ────────────────────────────────────────────────────
    # Record that poll completed, and ping the external dead-man's-switch (if
    # configured) — the per-minute poll is the natural liveness signal.
    _store.record_job_success("poll", _now_fn())
    if config.monitoring.heartbeat_url:
        from commutecompass.monitoring import ping_heartbeat

        ping_heartbeat(config.monitoring.heartbeat_url)


def _same_journey(old: Plan, new: Plan) -> bool:
    """Compare structure and genuine schedules, not observation-time placeholders.

    Walking/driving/bicycling step times are not schedules. Directions payloads
    establish which transit and route-level times were explicit; the model only
    has a departure-valid flag, not an arrival/route-time validity flag. Legacy
    payload-free routes retain their stored schedule comparisons. Trust and GTFS
    identity themselves are deliberately not journey fields.
    """
    if old.route is None or new.route is None:
        return False

    def explicit_time(plan: Plan, value: datetime, field: str, *, transit: bool) -> bool:
        from commutecompass.routing import _scheduled_time

        assert plan.route is not None
        payload = plan.route.raw_provider_payload
        if payload is None or payload.get("status") != "OK":
            return not plan.route.approximate
        for route in payload.get("routes", []):
            legs = route.get("legs", [])
            if transit:
                candidates = [step.get("transit_details", {})
                              for leg in legs for step in leg.get("steps", [])
                              if step.get("travel_mode") == "TRANSIT"]
            else:
                candidates = [legs[0] if field == "departure_time" else legs[-1]] if legs else []
            for candidate in candidates:
                timestamp = _scheduled_time(candidate.get(field), UTC)
                if timestamp is not None and timestamp == value.astimezone(UTC):
                    return True
        return False

    def journey(plan: Plan) -> object:
        assert plan.route is not None
        location = plan.event.location_resolved
        return (
            (location.kind, location.value, location.lat, location.lon) if location else None,
            plan.route.total_duration_seconds,
            [(leg.mode, leg.system, leg.line, leg.headsign,
              leg.duration_seconds, leg.departure_stop, leg.arrival_stop)
             for leg in plan.route.legs],
        )

    if journey(old) != journey(new):
        return False
    for field, attribute in (("departure_time", "depart_at"), ("arrival_time", "arrive_at")):
        old_time = getattr(old.route, attribute)
        new_time = getattr(new.route, attribute)
        if (explicit_time(old, old_time, field, transit=False)
                and explicit_time(new, new_time, field, transit=False)
                and old_time.astimezone(UTC) != new_time.astimezone(UTC)):
            return False
        for old_leg, new_leg in zip(old.route.legs, new.route.legs, strict=True):
            if old_leg.mode != "TRANSIT":
                continue
            if (not (old_leg.scheduled_departure_valid or new_leg.scheduled_departure_valid)
                    and old.route.raw_provider_payload is None
                    and new.route.raw_provider_payload is None):
                continue
            old_time = getattr(old_leg, attribute)
            new_time = getattr(new_leg, attribute)
            if (explicit_time(old, old_time, field, transit=True)
                    and explicit_time(new, new_time, field, transit=True)
                    and old_time.astimezone(UTC) != new_time.astimezone(UTC)):
                return False
    return True


def _retain_replan_safety(old: Plan, new: Plan) -> Plan:
    """Keep same-journey padding and the user's prep interval, with UTC deltas.

    New travel/weather timing remains authoritative. No GTFS identity is copied
    onto a Directions result. A genuinely different journey uses its own padding.
    """
    updated = new.model_copy(deep=True)
    if _same_journey(old, new) and old.realtime_buffer_minutes > new.realtime_buffer_minutes:
        delta = timedelta(minutes=old.realtime_buffer_minutes - new.realtime_buffer_minutes)
        for field in ("leave_at", "prep_at"):
            value = getattr(updated, field)
            if value is not None:
                setattr(updated, field, (value.astimezone(UTC) - delta).astimezone(value.tzinfo))
        updated.realtime_buffer_minutes = old.realtime_buffer_minutes
        updated.realtime_reason = old.realtime_reason
    if old.prep_at is not None and old.leave_at is not None and updated.leave_at is not None:
        interval = old.leave_at.astimezone(UTC) - old.prep_at.astimezone(UTC)
        updated.prep_at = (updated.leave_at.astimezone(UTC) - interval).astimezone(
            old.prep_at.tzinfo
        )
    return updated


def _location_update_significant(old_plan: Plan, new_plan: Plan) -> bool:
    """Stricter check used only for Phase 5 location-driven updates.

    Require leave_at to differ by at least _REPLAN_THRESHOLD_SECONDS. Leg-set
    changes alone (e.g. Mixed vs Subway-only at the same leave time) are NOT
    significant here — they're typically search noise between near-equivalent
    options and would spam the user once a minute. Alert-driven service
    updates (Phase 4) still use _route_significantly_different.
    """
    if new_plan.route is None:
        return False
    if old_plan.route is None:
        return True
    if old_plan.leave_at is None or new_plan.leave_at is None:
        return False
    diff = abs((new_plan.leave_at - old_plan.leave_at).total_seconds())
    return diff >= _REPLAN_THRESHOLD_SECONDS


def _route_significantly_different(old_plan: Plan, new_plan: Plan) -> bool:
    """Return True if new_plan's timing or legs differ meaningfully from old_plan."""
    if old_plan.route is None or new_plan.route is None:
        # If either had no route, any replan with a route is significant
        return new_plan.route is not None

    # Check timing threshold
    if old_plan.leave_at is not None and new_plan.leave_at is not None:
        diff = abs((new_plan.leave_at - old_plan.leave_at).total_seconds())
        if diff >= _REPLAN_THRESHOLD_SECONDS:
            return True

    # Check leg lines/systems
    old_lines = {(leg.system, leg.line) for leg in old_plan.route.legs if leg.mode == "TRANSIT"}
    new_lines = {(leg.system, leg.line) for leg in new_plan.route.legs if leg.mode == "TRANSIT"}
    if old_lines != new_lines:
        return True

    return False


def _schedule_pings_for_plan(plan: Plan, store: "Store", now: "datetime") -> None:
    """Schedule prep and leave pings for a plan (skip if already past)."""
    from commutecompass.format import format_prep_ping, format_leave_ping
    from uuid import uuid4

    if plan.leave_at is not None and plan.leave_at > now:
        leave_msg = format_leave_ping(plan)
        store.schedule_ping(
            PingEntry(
                id=str(uuid4()),
                event_id=plan.event.id,
                kind="leave",
                fire_at=plan.leave_at,
                fired=False,
                message=leave_msg,
            )
        )

    if plan.prep_at is not None and plan.prep_at > now:
        prep_msg = format_prep_ping(plan)
        store.schedule_ping(
            PingEntry(
                id=str(uuid4()),
                event_id=plan.event.id,
                kind="prep",
                fire_at=plan.prep_at,
                fired=False,
                message=prep_msg,
            )
        )
