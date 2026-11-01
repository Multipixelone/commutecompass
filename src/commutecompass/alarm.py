"""Wake alarm computation + publish to Home Assistant.

See ``docs/alarm-design.md``.  commutecompass only *publishes* the computed
morning wake time (and the next prep time used by HA's nap cap); HA owns nap
logic and the final "next alarm" selection, and ``alarm daemon`` rings it.

Wake rule (Q2/Q8)::

    wake = min(first_event.prep_at - wake_lead, fallback_time on that day)
    no plan-able events -> fallback_time

Target logical day: today's wake if it is still in the future, else
tomorrow's.  All arithmetic is done in absolute (UTC) time so DST transition
days stay correct; wall-clock values are rendered in America/New_York.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Optional

from commutecompass.timeutil import NYC_TZ, logical_day_bounds_nyc, now_nyc, to_nyc

if TYPE_CHECKING:
    from commutecompass.config import Config
    from commutecompass.models import Plan
    from commutecompass.store import Store

logger = logging.getLogger(__name__)

# Provider of plans for a logical day, keyed by that day's start datetime.
PlansProvider = Callable[[datetime], "list[Plan]"]


class AlarmError(Exception):
    """Base class for alarm failures surfaced to the CLI."""


class AlarmConfigError(AlarmError):
    """Alarm misconfigured (HA not set up, malformed script id, …)."""


@dataclass(frozen=True)
class WakeResult:
    """Outcome of :func:`compute_wake`."""

    wake_at: datetime
    day: date
    source: Literal["event", "fallback"]
    # Plan whose prep_at drove the wake (None for fallback).
    plan: Optional["Plan"]
    # Earliest future prep_at (today's or target day's plans) — HA nap cap.
    next_prep_at: Optional[datetime]
    next_event: str


def _earliest_prep_plan(plans: "list[Plan]", *, after: Optional[datetime] = None) -> Optional["Plan"]:
    """Plan with the smallest prep_at (non-errored), optionally strictly after ``after``."""
    best: Optional[Plan] = None
    for p in plans:
        if p.prep_at is None or p.error is not None:
            continue
        if after is not None and p.prep_at.astimezone(UTC) <= after.astimezone(UTC):
            continue
        if best is None or best.prep_at is None or p.prep_at.astimezone(UTC) < best.prep_at.astimezone(UTC):
            best = p
    return best


def fallback_for_day(day_start: datetime, fallback: time) -> datetime:
    """``fallback`` wall-clock on the logical day starting at ``day_start``.

    A fallback earlier than the day-start hour (e.g. ``01:00``) belongs to the
    following calendar date, matching the logical-day convention.
    """
    d = to_nyc(day_start).date()
    candidate = datetime.combine(d, fallback, tzinfo=NYC_TZ)
    if candidate.astimezone(UTC) < day_start.astimezone(UTC):
        candidate = datetime.combine(d + timedelta(days=1), fallback, tzinfo=NYC_TZ)
    return candidate


def wake_for_day(
    day_start: datetime, plans: "list[Plan]", config: "Config"
) -> tuple[datetime, Optional["Plan"]]:
    """Return (wake_at, driving plan) for one logical day."""
    fallback = fallback_for_day(day_start, config.alarm.fallback_clock())
    first = _earliest_prep_plan(plans)
    if first is None or first.prep_at is None:
        return fallback, None
    lead = timedelta(minutes=config.alarm.wake_lead_minutes)
    # Absolute arithmetic: aware-datetime subtraction in one zone is wall-clock.
    wake = (first.prep_at.astimezone(UTC) - lead).astimezone(NYC_TZ)
    if wake.astimezone(UTC) >= fallback.astimezone(UTC):
        return fallback, None
    return wake, first


def compute_wake(
    config: "Config",
    now: datetime,
    plans_for_day: PlansProvider,
) -> WakeResult:
    """Compute the next morning wake.

    ``plans_for_day(day_start)`` returns plans for the logical day beginning
    at ``day_start``; it is called lazily (tomorrow is only planned when
    today's wake has already passed).
    """
    now_utc = now.astimezone(UTC)
    today_start, _ = logical_day_bounds_nyc(now)
    today_plans = plans_for_day(today_start)
    wake, plan = wake_for_day(today_start, today_plans, config)
    day_start = today_start
    target_plans = today_plans
    if wake.astimezone(UTC) <= now_utc:
        tomorrow_start, _ = logical_day_bounds_nyc(today_start + timedelta(hours=36))
        target_plans = plans_for_day(tomorrow_start)
        wake, plan = wake_for_day(tomorrow_start, target_plans, config)
        day_start = tomorrow_start

    pool = list(today_plans) if day_start == today_start else [*today_plans, *target_plans]
    nxt = _earliest_prep_plan(pool, after=now)
    from commutecompass.format import _sanitize_text

    return WakeResult(
        wake_at=to_nyc(wake),
        day=to_nyc(day_start).date(),
        source="event" if plan is not None else "fallback",
        plan=plan,
        next_prep_at=to_nyc(nxt.prep_at) if nxt is not None and nxt.prep_at is not None else None,
        next_event=_sanitize_text(nxt.event.title) if nxt is not None else "",
    )


def split_service(service: str) -> tuple[str, str]:
    """Split ``domain.service`` or raise :class:`AlarmConfigError`."""
    domain, sep, name = service.partition(".")
    if not sep or not domain or not name:
        raise AlarmConfigError(f"{service!r} is not of the form 'domain.service'")
    return domain, name


def require_ha(config: "Config") -> tuple[str, str]:
    """Return (base_url, token) or raise :class:`AlarmConfigError`."""
    base_url = config.home_assistant.base_url
    token = config.home_assistant_token
    if not (config.home_assistant.enabled and base_url and token):
        raise AlarmConfigError(
            "alarm needs [home_assistant] enabled with base_url and HOME_ASSISTANT_TOKEN"
        )
    return base_url, token


def _default_provider(
    config: "Config",
    planned: Mapping[date, "list[Plan]"],
    store: Optional["Store"],
    now: datetime,
) -> PlansProvider:
    """Plans provider: explicit ``planned`` → stored plans (once morning ran) → fresh plan."""

    def provider(day_start: datetime) -> "list[Plan]":
        key = to_nyc(day_start).date()
        if key in planned:
            return list(planned[key])
        _, day_end = logical_day_bounds_nyc(day_start)
        # When this day's latest possible wake has passed, no event can make
        # it future again. Only stored plans are needed for today's nap cap;
        # don't refetch yesterday/today just to choose tomorrow.
        fallback = fallback_for_day(day_start, config.alarm.fallback_clock())
        if fallback.astimezone(UTC) <= now.astimezone(UTC):
            return store.today_plans() if store is not None else []
        if store is not None:
            ran = store.get_job_heartbeat("morning")
            if ran is not None and to_nyc(ran) >= day_start:
                start_utc, end_utc = day_start.astimezone(UTC), day_end.astimezone(UTC)
                return [
                    p for p in store.today_plans()
                    if start_utc <= p.event.start.astimezone(UTC) <= end_utc
                ]
        from commutecompass.jobs.tomorrow import plan_window

        plans = plan_window(config, day_start, day_end)
        if plans is None:
            raise AlarmError(f"calendar fetch failed for {key}")
        return plans

    return provider


def build_payload(result: WakeResult) -> dict[str, object]:
    """HA script fields for ``publish_script``."""
    return {
        "wake_at": result.wake_at.isoformat(),
        "next_prep_at": result.next_prep_at.isoformat() if result.next_prep_at else "",
        "next_event": result.next_event,
    }


def publish(
    config: "Config",
    *,
    now: Optional[datetime] = None,
    planned: Optional[Mapping[date, "list[Plan]"]] = None,
    store: Optional["Store"] = None,
    dry_run: bool = False,
    plans_for_day: Optional[PlansProvider] = None,
) -> tuple[WakeResult, bool]:
    """Compute the wake and POST it to ``[alarm].publish_script``.

    Returns ``(result, pushed)``.  ``pushed`` is False on dry-run or HA
    failure.  Raises :class:`AlarmConfigError` when HA is not configured (not
    on dry-run) and :class:`AlarmError` when planning fails.
    """
    from commutecompass.ha_client import call_service

    _now = now or now_nyc()
    if not dry_run:
        base_url, token = require_ha(config)
        domain, name = split_service(config.alarm.publish_script)
    if store is None and plans_for_day is None:
        from commutecompass.store import Store

        store = Store(config.paths.db_path)
        store.init_schema()
    provider = plans_for_day or _default_provider(config, planned or {}, store, _now)
    result = compute_wake(config, _now, provider)
    logger.info(
        "alarm: wake_at=%s (%s) next_prep_at=%s",
        result.wake_at.isoformat(),
        result.source,
        result.next_prep_at.isoformat() if result.next_prep_at else "-",
    )
    if dry_run:
        return result, False
    ok = call_service(base_url, token, domain, name, data=build_payload(result))
    if not ok:
        logger.warning("alarm: publish to %s failed", config.alarm.publish_script)
    return result, ok


def publish_safe(
    config: "Config",
    *,
    store: Optional["Store"] = None,
    planned: Optional[Mapping[date, "list[Plan]"]] = None,
) -> None:
    """:func:`publish` for background callers (poll/tomorrow): never raises."""
    try:
        publish(config, store=store, planned=planned)
    except Exception as exc:  # noqa: BLE001 — alarm must never break a job
        logger.warning("alarm publish failed: %s", exc)
