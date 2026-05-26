"""Poll loop job."""

from __future__ import annotations

import logging
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

# How long to reuse a fetched MTA alert set inside the poll loop (in seconds).
# Only applied when the caller did not inject a fetch_alerts_fn (tests bypass).
_MTA_CACHE_TTL_SECONDS = 180

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
    2. Fire any due pings; mark fired on success
    3. Fetch fresh MTA alerts
    4. For each new alert affecting a today-plan:
       a. Re-plan the event
       b. If route changed significantly: send service_update, upsert plan,
          cancel old pings, schedule new ones
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
    in_quiet_hours = (
        quiet_start is not None
        and quiet_end is not None
        and is_within_quiet_hours(now, quiet_start, quiet_end)
    )

    # ── Phase 2: fire due pings ───────────────────────────────────────────────
    # Atomic claim-then-send: every ping we try to send is first claimed in a
    # single UPDATE so two concurrent poll runs cannot both pick up the same
    # row.  Failure modes are surfaced as warnings and counted in the summary;
    # we deliberately do NOT retry on send failure (no retry storm).
    due_pings = _store.pending_pings(before=now)
    for ping in due_pings:
        # During quiet hours, only fire 'leave' pings — leave the row unfired
        # so a future poll (after quiet hours end) can still claim it.
        if in_quiet_hours and ping.kind != "leave":
            logger.debug("Suppressing %s ping during quiet hours", ping.kind)
            continue

        if not _store.claim_ping(ping.id, now):
            logger.debug("Ping %s already claimed by another runner", ping.id)
            continue

        sent_ok = _notifier.send(ping.message)
        if sent_ok:
            logger.info("Fired ping %s (%s)", ping.id, ping.kind)
        else:
            logger.warning(
                "Send failed for claimed ping %s (%s) — not retrying",
                ping.id,
                ping.kind,
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
    if _use_alerts_cache and _alerts_cache is not None:
        cached_at, cached_urls, cached_alerts = _alerts_cache
        if cached_urls == url_key and (now - cached_at).total_seconds() < _MTA_CACHE_TTL_SECONDS:
            alerts = cached_alerts
            logger.debug(
                "Reusing cached MTA alerts (%d, age %.0fs)",
                len(alerts),
                (now - cached_at).total_seconds(),
            )
    if alerts is None:
        alerts = _fetch_alerts(
            subway_url=url_key[0],
            lirr_url=url_key[1],
            bus_url=url_key[2],
        )
        logger.debug("Fetched %d MTA alerts", len(alerts))
        if _use_alerts_cache:
            _alerts_cache = (now, url_key, alerts)

    # ── Phase 4: process new affecting alerts ─────────────────────────────────
    today_plans = _store.today_plans()

    for plan in today_plans:
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

            # Determine if the change warrants a service update
            route_changed = _route_significantly_different(plan, new_plan)

            if route_changed:
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

                # Upsert new plan
                _store.upsert_plan(new_plan)

                # Cancel old pings and schedule new ones
                _store.cancel_pings(plan.event.id)
                _schedule_pings_for_plan(new_plan, _store, now)
            else:
                # No significant change but still mark seen
                logger.debug(
                    "Alert %s affects event %s but route unchanged — marking seen",
                    alert.id,
                    plan.event.id,
                )

            _store.mark_alert_seen(alert.id, plan.event.id)

    # ── Phase 5: location-driven replan close to leave time ──────────────────
    if config.home_assistant.enabled and not in_quiet_hours:
        from commutecompass.format import format_location_update

        window_seconds = config.home_assistant.replan_window_minutes * 60
        for plan in _store.today_plans():
            if plan.leave_at is None or plan.leave_at <= now:
                continue
            if (plan.leave_at - now).total_seconds() > window_seconds:
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

            if not _location_update_significant(plan, new_plan):
                continue

            msg = format_location_update(plan, new_plan)
            if _notifier.send(msg):
                logger.info("Sent location update for event %s", plan.event.id)
            else:
                logger.warning("Location update send failed for event %s", plan.event.id)

            _store.upsert_plan(new_plan)
            _store.cancel_pings(plan.event.id)
            _schedule_pings_for_plan(new_plan, _store, now)


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
