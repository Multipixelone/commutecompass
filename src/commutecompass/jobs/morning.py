"""Morning digest job.

Sequence (§6.14):
1. Compute [start, end] window: logical day 02:00 to 01:59 NYC
2. Fetch calendar events via calendar_client
3. For each event: plan = plan_event(...); store.upsert_plan(plan)
4. Cancel stale pings for events that no longer exist
5. For each plan with non-None leave_at: schedule prep + leave pings
   (skip fire_at < now — already past for late-morning runs)
6. Pull MTA alerts affecting today's planned routes
7. Build digest with format_digest; send via Telegram
8. Log structured summary to journal
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from commutecompass.calendar_client import AuthError, CalendarClient
from commutecompass.config import Config
from commutecompass.format import format_digest, format_leave_ping, format_prep_ping
from commutecompass.mta import fetch_alerts
from commutecompass.notify import build_notifier
from commutecompass.planner import plan_event
from commutecompass.store import Store
from commutecompass.timeutil import logical_day_bounds_nyc, now_nyc
from commutecompass.venues import VenueRegistry

if TYPE_CHECKING:
    from commutecompass.llm import OpencodeGoClient

from commutecompass.models import (
    Alert,
    CalendarSpec,
    Event,
    PingEntry,
    Plan,
    ZoneInfo,
)

logger = logging.getLogger(__name__)


def run(config: Config) -> None:  # noqa: C901
    """Run the morning digest job.

    Args:
        config: Validated application configuration.
    """
    _now = now_nyc()
    today_start, today_end = logical_day_bounds_nyc(_now)

    # ── 1. Fetch today's calendar events ─────────────────────────────────────
    calendar_client = CalendarClient(
        client_secret_json=config.google_oauth_client_secret_json,
        token_path=config.paths.oauth_token_path,
    )
    events: list[Event] = []
    auth_failed = False
    try:
        events = calendar_client.fetch_events(
            calendars=[
                CalendarSpec(id=cal.id, name=cal.name, enabled=cal.enabled)
                for cal in config.calendars
            ],
            start=today_start,
            end=today_end,
        )
    except AuthError as exc:
        # An expired/invalid OAuth token degrades to "no events" — but unlike a
        # transient API blip it will NOT fix itself, so surface it loudly in the
        # digest footer instead of letting the user think their day is empty.
        logger.error("Calendar auth failed — re-auth needed: %s", exc)
        auth_failed = True
        events = []
    except Exception as exc:
        logger.error("Failed to fetch calendar events: %s", exc)
        # Continue with empty events list — digest will reflect no events
        events = []

    logger.info("morning job: fetched %d events", len(events))

    # ── 2. Plan each event ────────────────────────────────────────────────────
    store = Store(config.paths.db_path)
    store.init_schema()

    # Refresh current location once so plan_event can pick it up via effective_origin.
    ha_zones: dict[str, ZoneInfo] = {}
    if config.home_assistant.enabled:
        from commutecompass.ha_client import fetch_location as _ha_fetch
        from commutecompass.ha_client import fetch_zones as _ha_fetch_zones

        try:
            loc = _ha_fetch(
                config.home_assistant.base_url,
                config.home_assistant.entity_id,
                config.home_assistant_token,
                min_accuracy_m=float(config.home_assistant.min_gps_accuracy_meters),
            )
        except Exception as exc:
            logger.warning("HA fetch raised in morning: %s", exc)
            loc = None
        if loc is not None:
            store.upsert_current_location(loc)
            logger.debug(
                "morning ha_pull: ok lat=%.5f lon=%.5f zone=%s", loc.lat, loc.lon, loc.zone
            )

        try:
            ha_zones = _ha_fetch_zones(
                config.home_assistant.base_url,
                config.home_assistant_token,
            )
            logger.debug("morning ha_zones: %d zones loaded", len(ha_zones))
        except Exception as exc:
            logger.warning("HA fetch_zones raised in morning: %s", exc)
            ha_zones = {}

    venue_registry = VenueRegistry.load(Path(config.paths.venues_file))

    # Lazily create LLM client only if we have events with locations
    from commutecompass.llm import OpencodeGoClient
    llm_client = OpencodeGoClient(
        endpoint=config.opencode_go.endpoint,
        token=config.opencode_go_token,
        model=config.opencode_go.model,
    )

    plans: list[Plan] = []
    for event in events:
        plan = _plan_event_safe(event, config, venue_registry, store, llm_client, ha_zones)
        store.upsert_plan(plan)
        plans.append(plan)

    logger.info("morning job: planned %d events", len(plans))

    # ── 3. Cancel stale pings for events that no longer exist ─────────────────
    event_ids_today = {e.id for e in events}
    for existing_plan in store.today_plans():
        if existing_plan.event.id not in event_ids_today:
            cancelled = store.cancel_pings(existing_plan.event.id)
            logger.debug(
                "cancelled %d stale pings for removed event %s",
                cancelled,
                existing_plan.event.id,
            )

    # ── 4. Schedule future prep + leave pings ─────────────────────────────────
    for plan in plans:
        if plan.leave_at is None:
            continue

        # prep ping
        if plan.prep_at and plan.prep_at > _now:
            message = format_prep_ping(plan)
            ping = PingEntry(
                id=str(uuid.uuid4()),
                event_id=plan.event.id,
                kind="prep",
                fire_at=plan.prep_at,
                fired=False,
                message=message,
            )
            store.schedule_ping(ping)
            logger.debug(
                "scheduled prep ping for event %s at %s",
                plan.event.id,
                plan.prep_at,
            )

        # leave ping
        if plan.leave_at > _now:
            message = format_leave_ping(plan)
            ping = PingEntry(
                id=str(uuid.uuid4()),
                event_id=plan.event.id,
                kind="leave",
                fire_at=plan.leave_at,
                fired=False,
                message=message,
            )
            store.schedule_ping(ping)
            logger.debug(
                "scheduled leave ping for event %s at %s",
                plan.event.id,
                plan.leave_at,
            )

    # ── 5. Pull MTA alerts affecting today's routes ──────────────────────────
    all_alerts: list[Alert] = []
    try:
        all_alerts = fetch_alerts(
            subway_url=config.mta.subway_alerts_url,
            lirr_url=config.mta.lirr_alerts_url,
            bus_url=config.mta.bus_alerts_url,
        )
    except Exception as exc:
        logger.warning("Failed to fetch MTA alerts: %s", exc)

    # Filter to those affecting today's planned routes
    from commutecompass.llm import OpencodeGoClient
    from commutecompass.mta import select_actionable_alerts

    llm_client = OpencodeGoClient(
        endpoint=config.opencode_go.endpoint,
        token=config.opencode_go_token,
        model=config.opencode_go.model,
    )

    affecting_alerts: list[Alert] = []
    for plan in plans:
        if plan.route and plan.leave_at:
            affected = select_actionable_alerts(
                all_alerts,
                plan.route,
                at_time=plan.leave_at,
                llm=llm_client,
            )
            for alert in affected:
                if alert not in affecting_alerts:
                    affecting_alerts.append(alert)

    logger.info(
        "morning job: %d affecting alerts out of %d total",
        len(affecting_alerts),
        len(all_alerts),
    )

    # ── 6. Build and send digest ──────────────────────────────────────────────
    poll_stale = _poll_heartbeat_stale(store, config, _now)
    ops_notes = _operations_notes(
        plans, all_alerts, auth_failed=auth_failed, poll_stale=poll_stale
    )
    digest = format_digest(plans, affecting_alerts, operations_notes=ops_notes)
    notifier = build_notifier(config)
    sent = notifier.send(digest)
    if sent:
        logger.info("morning job: digest sent successfully")
    else:
        logger.warning("morning job: digest send failed")

    # Record morning's own heartbeat and ping the external dead-man's-switch.
    store.record_job_success("morning", _now)
    if config.monitoring.heartbeat_url:
        from commutecompass.monitoring import ping_heartbeat

        ping_heartbeat(config.monitoring.heartbeat_url)

    # ── 7. Log structured summary ────────────────────────────────────────────
    unresolved = sum(1 for p in plans if p.error == "location_unresolved")
    no_route = sum(1 for p in plans if p.error == "no_route")
    too_imminent = sum(1 for p in plans if p.error == "too_imminent")
    logger.info(
        "morning_run_summary: events=%d plans=%d unresolved=%d no_route=%d "
        "too_imminent=%d alerts=%d digest_sent=%s auth_failed=%s",
        len(events),
        len(plans),
        unresolved,
        no_route,
        too_imminent,
        len(affecting_alerts),
        sent,
        auth_failed,
    )


def _poll_heartbeat_stale(store: Store, config: Config, now: datetime) -> bool:
    """True if the poll loop has not completed within the staleness threshold.

    Poll runs every minute, so by morning a healthy poll heartbeat is seconds
    old.  A stale (or missing) heartbeat means the per-minute timer is dead and
    no leave/prep alarms will fire today — worth shouting about in the digest.
    """
    from datetime import timedelta

    last = store.get_job_heartbeat("poll")
    if last is None:
        return True
    threshold = timedelta(minutes=config.monitoring.poll_staleness_minutes)
    return (now - last) > threshold


def _operations_notes(
    plans: list[Plan],
    all_alerts: list[Alert],
    *,
    auth_failed: bool = False,
    poll_stale: bool = False,
) -> list[str]:
    """Build the "Operations:" footer items for the morning digest.

    Surfaces degraded-service signals that today would only land in stderr:
    calendar auth that needs re-running, a dead poll timer, MTA feeds that went
    silent after retries, plans whose location couldn't be resolved, and plans
    that were stored with "too_imminent" / "no_route".
    """
    notes: list[str] = []

    # Calendar auth failure first — without it the whole digest is empty and the
    # user would otherwise have no idea their token lapsed.
    if auth_failed:
        notes.append("Calendar auth expired — re-run `commutecompass oauth`")

    # A dead poll timer means no alarms will fire today.
    if poll_stale:
        notes.append("Poll loop has not run recently — alarms may not fire (check the timer)")

    # Per-feed MTA failures reported by fetch_alerts (set as an attribute).
    failed_feeds: list[str] = getattr(fetch_alerts, "last_failed_systems", [])
    for system in failed_feeds:
        notes.append(f"{system} alerts unavailable — retried and gave up")

    unresolved_titles = [
        p.event.title for p in plans if p.error == "location_unresolved"
    ]
    if unresolved_titles:
        names = ", ".join(unresolved_titles[:3])
        more = "" if len(unresolved_titles) <= 3 else f" (+{len(unresolved_titles) - 3} more)"
        notes.append(f"Unresolved location: {names}{more}")

    too_imminent = sum(1 for p in plans if p.error == "too_imminent")
    if too_imminent:
        notes.append(
            f"{too_imminent} event(s) too imminent for a prep window"
        )

    return notes


def _plan_event_safe(
    event: Event,
    config: Config,
    venue_registry: VenueRegistry,
    store: Store,
    llm_client: "OpencodeGoClient",
    ha_zones: dict[str, ZoneInfo] | None = None,
) -> Plan:
    """Call plan_event with error handling, returning an error Plan on failure."""
    try:
        return plan_event(
            event,
            config=config,
            venues=venue_registry,
            store=store,
            llm=llm_client,
            ha_zones=ha_zones,
        )
    except Exception as exc:
        logger.warning(
            "plan_event failed for event %s (%s): %s",
            event.id,
            event.title,
            exc,
        )
        return Plan(event=event, error=f"internal_error: {exc}")
