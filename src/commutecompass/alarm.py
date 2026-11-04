"""Wake alarm computation + publish to Home Assistant.

See ``docs/alarm-design.md``.  commutecompass only *publishes* the computed
morning wake time (and the next prep time used by HA's nap cap); HA owns nap
logic and the final "next alarm" selection, and ``alarm daemon`` rings it.

Wake rule (Q2/Q8)::

    wake = min(first_alarm(first_event), fallback_time on that day)
    first_alarm = leave_at - window.first_alarm_lead   (healthy wake window)
                = prep_at - wake_lead                  (window disabled / no leave_at)
    no plan-able events -> fallback_time

Target logical day: today's wake if it is still in the future, else
tomorrow's, then the following day's if both have elapsed. All arithmetic is done
in absolute (UTC) time so DST transition
days stay correct; wall-clock values are rendered in America/New_York.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal

from commutecompass.alarm_contract import (
    ScheduleError,
    ScheduleSnapshot,
    build_publish_payload,
    new_schedule,
)
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
    plan: Plan | None
    # Earliest future prep_at across today/tomorrow, minus wake lead — HA nap cap.
    # For wake-window plans this is the first window alarm instead.
    next_prep_at: datetime | None
    next_event: str
    # Buffered leave deadline anchoring a commute wake window
    # (``wake_at == deadline_at - first_alarm_lead``); None for fallback/prep wakes.
    deadline_at: datetime | None = None


def _window_deadline(plan: Plan, config: Config) -> datetime | None:
    """Healthy plan's buffered leave deadline; None = plain prep wake.

    ``depart_at`` is informational only: safety, weather and realtime buffers
    are already included in ``leave_at`` and must move the entire wake window.
    """
    if not config.alarm.window.enabled or plan.error is not None:
        return None
    return plan.leave_at


def _first_alarm(plan: Plan, config: Config) -> datetime | None:
    """First alarm a healthy plan needs: window start, else prep minus lead."""
    deadline = _window_deadline(plan, config)
    if deadline is not None:
        lead = timedelta(minutes=config.alarm.window.first_alarm_lead_minutes)
        return (deadline.astimezone(UTC) - lead).astimezone(NYC_TZ)
    if plan.prep_at is None:
        return None
    lead = timedelta(minutes=config.alarm.wake_lead_minutes)
    return (plan.prep_at.astimezone(UTC) - lead).astimezone(NYC_TZ)


def _earliest_alarm_plan(
    plans: list[Plan], config: Config, *, after: datetime | None = None,
) -> tuple[Plan, datetime] | None:
    """Non-errored plan with the earliest first alarm, prep strictly after ``after``."""
    best: tuple[Plan, datetime] | None = None
    for p in plans:
        if p.prep_at is None or p.error is not None:
            continue
        if after is not None and p.prep_at.astimezone(UTC) <= after.astimezone(UTC):
            continue
        alarm = _first_alarm(p, config)
        if alarm is None:
            continue
        if best is None or alarm.astimezone(UTC) < best[1].astimezone(UTC):
            best = (p, alarm)
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
    day_start: datetime, plans: list[Plan], config: Config, *, now: datetime | None = None
) -> tuple[datetime, Plan | None]:
    """Return the day's first wake, retaining historical evidence it has elapsed.

    An unresolved event that has already started cannot require a future wake:
    its start is a conservative upper bound on prep. Keep that bound rather than
    dropping the event and inventing a second wake from a later appointment.
    Unknown future timings still fail closed, even after the day's fallback.
    """
    wake = fallback_for_day(day_start, config.alarm.fallback_clock())
    first: Plan | None = None
    lead = timedelta(minutes=config.alarm.wake_lead_minutes)
    now_utc = now.astimezone(UTC) if now is not None else None
    for p in plans:
        prep = p.prep_at
        if p.error is not None or prep is None:
            if (
                now_utc is not None
                and p.error == "too_imminent"
                and prep is not None
                and p.leave_at is not None
                and prep.astimezone(UTC) <= p.leave_at.astimezone(UTC) <= now_utc
                and p.leave_at.astimezone(UTC) <= p.event.start.astimezone(UTC)
            ):
                pass  # Known past prep remains evidence of the first wake.
            elif now_utc is not None and p.event.start.astimezone(UTC) <= now_utc:
                prep = p.event.start  # Historical uncertainty cannot cap a future nap.
            else:
                raise AlarmError(f"incomplete planning for {day_start.date()}")
        assert prep is not None
        # Absolute arithmetic: same-zone datetime subtraction is wall-clock.
        candidate = _first_alarm(p, config) if p.error is None else None
        if candidate is None:
            candidate = (prep.astimezone(UTC) - lead).astimezone(NYC_TZ)
        if candidate.astimezone(UTC) < wake.astimezone(UTC):
            wake, first = candidate, p
    return wake, first


def compute_wake(
    config: Config,
    now: datetime,
    plans_for_day: PlansProvider,
) -> WakeResult:
    """Compute the next morning wake.

    Both days are planned: today's wake wins while future, and the nap cap
    considers the earliest future prep across today and tomorrow, extending to
    the third logical day only when both wakes have elapsed (possible with an
    early event and a large lead). Incomplete planning in any fetched day raises
    rather than publishing a potentially unsafe later fallback.
    """
    now_utc = now.astimezone(UTC)
    today_start, _ = logical_day_bounds_nyc(now)
    today_plans = plans_for_day(today_start)
    tomorrow_start, _ = logical_day_bounds_nyc(today_start + timedelta(hours=36))
    tomorrow_plans = plans_for_day(tomorrow_start)
    # Even after today's fallback, today's afternoon prep still caps naps.
    # Validate both snapshots so partial planning never clears that cap.
    tomorrow_wake, tomorrow_plan = wake_for_day(tomorrow_start, tomorrow_plans, config, now=now)
    wake, plan = wake_for_day(today_start, today_plans, config, now=now)
    day_start = today_start
    if wake.astimezone(UTC) <= now_utc:
        wake, plan = tomorrow_wake, tomorrow_plan
        day_start = tomorrow_start
    all_plans = [*today_plans, *tomorrow_plans]
    if wake.astimezone(UTC) <= now_utc:
        # An early tomorrow event minus a large lead can already be past tonight.
        # Search one more logical day, still validating every fetched snapshot.
        day_start, _ = logical_day_bounds_nyc(tomorrow_start + timedelta(hours=36))
        later_plans = plans_for_day(day_start)
        wake, plan = wake_for_day(day_start, later_plans, config, now=now)
        all_plans.extend(later_plans)
    if wake.astimezone(UTC) <= now_utc:
        raise AlarmError("no future wake in three logical days")

    # Nap cap: the next first alarm (window start or prep minus lead).
    nxt = _earliest_alarm_plan(all_plans, config, after=now)
    from commutecompass.format import _sanitize_text

    deadline: datetime | None = None
    if plan is not None and _same_instant(_first_alarm(plan, config), wake):
        deadline = _window_deadline(plan, config)
    return WakeResult(
        wake_at=to_nyc(wake),
        day=to_nyc(day_start).date(),
        source="event" if plan is not None else "fallback",
        plan=plan,
        next_prep_at=nxt[1] if nxt is not None else None,
        next_event=_sanitize_text(nxt[0].event.title) if nxt is not None else "",
        deadline_at=to_nyc(deadline) if deadline is not None else None,
    )


def _same_instant(a: datetime | None, b: datetime) -> bool:
    return a is not None and a.astimezone(UTC) == b.astimezone(UTC)


def split_service(service: str) -> tuple[str, str]:
    """Split ``domain.service`` or raise :class:`AlarmConfigError`."""
    domain, sep, name = service.partition(".")
    if not sep or not domain or not name:
        raise AlarmConfigError(f"{service!r} is not of the form 'domain.service'")
    return domain, name


def require_ha(config: Config) -> tuple[str, str]:
    """Return (base_url, token) or raise :class:`AlarmConfigError`."""
    base_url = config.home_assistant.base_url
    token = config.home_assistant_token
    if not (config.home_assistant.enabled and base_url and token):
        raise AlarmConfigError(
            "alarm needs [home_assistant] enabled with base_url and HOME_ASSISTANT_TOKEN"
        )
    return base_url, token


def _default_provider(
    config: Config,
    planned: Mapping[date, list[Plan]],
    store: Store | None,
    now: datetime,
) -> PlansProvider:
    """Plans provider: explicit ``planned`` → stored plans (once morning ran) → fresh plan."""

    def provider(day_start: datetime) -> list[Plan]:
        key = to_nyc(day_start).date()
        if key in planned:
            return list(planned[key])
        _, day_end = logical_day_bounds_nyc(day_start)
        # Stored morning plans are not a complete calendar snapshot (new events
        # may have arrived). Fetch both days even after today's fallback so HA's
        # nap cap cannot silently lose an afternoon prep.
        from commutecompass.jobs.tomorrow import plan_window

        plans = plan_window(config, day_start, day_end)
        if plans is None:
            raise AlarmError(f"calendar fetch failed for {key}")
        return plans

    return provider


def build_payload(result: WakeResult, *, window: bool = False) -> dict[str, object]:
    """HA script fields for ``publish_script``.

    ``window`` adds ``deadline_at`` (empty = this wake opens no window), so
    HA scripts without the field keep working while the window is disabled.
    """
    payload: dict[str, object] = {
        "wake_at": result.wake_at.isoformat(),
        "next_prep_at": result.next_prep_at.isoformat() if result.next_prep_at else "",
        "next_event": result.next_event,
    }
    if window:
        payload["deadline_at"] = (
            result.deadline_at.isoformat() if result.deadline_at else ""
        )
    return payload


def build_schedule(
    result: WakeResult,
    config: Config,
    *,
    issued_at: datetime,
    schedule_id: str | None = None,
) -> ScheduleSnapshot:
    """v1 snapshot for ``result``: ``commute`` with a window deadline, else ``legacy``.

    The returned snapshot carries the publication identity (id + issued_at);
    retries must reuse it instead of building a new one.
    """
    lead_seconds = config.alarm.window.first_alarm_lead_minutes * 60
    try:
        return new_schedule(
            issued_at=issued_at,
            kind="commute" if result.deadline_at is not None else "legacy",
            wake_at=result.wake_at,
            deadline_at=result.deadline_at,
            next_prep_at=result.next_prep_at,
            schedule_id=schedule_id,
            first_lead_seconds=lead_seconds,
        )
    except ScheduleError as exc:
        raise AlarmError(f"cannot build alarm schedule v1: {exc}") from exc


def fetch_entity_state(
    base_url: str, token: str, entity_id: str, *, timeout: float = 5.0,
) -> str | None:
    """``GET /api/states/{entity_id}`` → raw ``state`` string, None on any failure."""
    import httpx

    url = f"{base_url.rstrip('/')}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("alarm: HA read of %s failed: %s", entity_id, exc)
        return None
    if response.status_code != 200:
        logger.warning("alarm: HA read of %s returned %d", entity_id, response.status_code)
        return None
    try:
        body = response.json()
    except ValueError:
        logger.warning("alarm: HA read of %s returned bad JSON", entity_id)
        return None
    state = body.get("state") if isinstance(body, dict) else None
    return state if isinstance(state, str) else None


def publish_v1(
    config: Config,
    schedule: ScheduleSnapshot,
    next_event: str,
    *,
    attempts: int = 2,
) -> str:
    """Publish ``schedule`` via ``publish_v1_script`` and confirm exact readback.

    Sends ``{"snapshot": <canonical string>, "next_event": ...}``; the HA
    script owns ordering (stale/conflicting publications are refused there).
    Acceptance is only ``schedule_entity`` holding the exact canonical string
    afterwards — HTTP success alone is not enough, and a lost response whose
    commit is visible counts as success.  Each of ``attempts`` (no sleep)
    resends the *same* snapshot.  Never falls back to, or writes, the legacy
    script.  Returns the snapshot string; raises :class:`AlarmError`.
    """
    from commutecompass.ha_client import call_service

    base_url, token = require_ha(config)
    domain, name = split_service(config.alarm.publish_v1_script)
    if not config.alarm.schedule_entity:
        raise AlarmConfigError("alarm.schedule_entity must be set for schedule_contract v1")
    try:
        payload = build_publish_payload(
            schedule, next_event,
            first_lead_seconds=config.alarm.window.first_alarm_lead_minutes * 60,
        )
    except ScheduleError as exc:
        raise AlarmError(f"invalid alarm schedule v1: {exc}") from exc
    snapshot = payload["snapshot"]
    data: dict[str, object] = dict(payload)
    observed: str | None = None
    for _ in range(max(1, attempts)):
        called = call_service(base_url, token, domain, name, data=data)
        observed = fetch_entity_state(base_url, token, config.alarm.schedule_entity)
        if observed == snapshot:
            logger.info("alarm: v1 schedule %s committed", schedule.id)
            return snapshot
        # Never log the raw helper value: it is arbitrary HA state.
        logger.warning(
            "alarm: v1 publish of %s not confirmed (call_ok=%s, readback=%s)",
            schedule.id, called,
            "missing" if observed is None else f"mismatch(len={len(observed)})",
        )
    raise AlarmError(
        f"alarm schedule v1 {schedule.id} not confirmed in {config.alarm.schedule_entity}"
    )


def publish(
    config: Config,
    *,
    now: datetime | None = None,
    planned: Mapping[date, list[Plan]] | None = None,
    store: Store | None = None,
    dry_run: bool = False,
    plans_for_day: PlansProvider | None = None,
) -> tuple[WakeResult, bool]:
    """Compute the wake and POST it to ``[alarm].publish_script``.

    Returns ``(result, pushed)``.  ``pushed`` is False on dry-run or HA
    failure.  With ``schedule_contract = "v1"`` the wake is published only
    via :func:`publish_v1` (never the legacy script), and an unconfirmed
    commit raises :class:`AlarmError` instead of returning False.  Raises :class:`AlarmConfigError` when HA is not configured (not
    on dry-run) and :class:`AlarmError` when planning fails.
    """
    from commutecompass.ha_client import call_service

    _now = now or now_nyc()
    if not dry_run:
        base_url, token = require_ha(config)
        v1 = config.alarm.schedule_contract == "v1"
        domain, name = split_service(
            config.alarm.publish_v1_script if v1 else config.alarm.publish_script
        )
    if store is None and plans_for_day is None:
        from commutecompass.store import Store

        store = Store(config.paths.db_path)
        store.init_schema()
    provider = plans_for_day or _default_provider(config, planned or {}, store, _now)
    result = compute_wake(config, _now, provider)
    logger.info(
        "alarm: wake_at=%s (%s) next_prep_at=%s deadline_at=%s computed_at=%s day=%s",
        result.wake_at.isoformat(),
        result.source,
        result.next_prep_at.isoformat() if result.next_prep_at else "-",
        result.deadline_at.isoformat() if result.deadline_at else "-",
        _now.isoformat(),
        result.day.isoformat(),
    )
    if dry_run:
        return result, False
    if config.alarm.schedule_contract == "v1":
        schedule = build_schedule(result, config, issued_at=_now)
        publish_v1(config, schedule, result.next_event)
        if store is not None:
            store.record_job_success("alarm_publish", _now)
        return result, True
    payload = build_payload(result, window=config.alarm.window.enabled)
    ok = call_service(base_url, token, domain, name, data=payload)
    if not ok:
        logger.warning("alarm: publish to %s failed", config.alarm.publish_script)
    elif store is not None:
        store.record_job_success("alarm_publish", _now)
    return result, ok


def publish_safe(
    config: Config,
    *,
    store: Store | None = None,
    planned: Mapping[date, list[Plan]] | None = None,
    now: datetime | None = None,
) -> None:
    """:func:`publish` for background callers (poll/tomorrow): never raises."""
    try:
        publish(config, store=store, planned=planned, now=now)
    except Exception as exc:  # noqa: BLE001 — alarm must never break a job
        logger.warning("alarm publish failed: %s", exc)
