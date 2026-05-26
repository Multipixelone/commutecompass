"""Tomorrow alarm push job.

Pull-model companion to the morning digest. Runs once in the evening
(typically ~21:00 NYC via a systemd timer) and pushes the next logical
day's earliest ``prep_at`` to a Home Assistant script. An iOS Shortcuts
automation polls HA later and sets a wake-up alarm against that value.

Sequence:
1. Compute the next logical day's [start, end] window.
2. Fetch tomorrow's calendar events via calendar_client.
3. Plan each event (no DB upsert — today's stored plans are kept untouched
   so the morning job remains the source of truth for the active day).
4. Pick the earliest non-None ``prep_at`` across planned events.
5. POST it to the configured HA script. No-op when [home_assistant.tomorrow]
   is disabled or there are no planned commutes.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from commutecompass.calendar_client import CalendarClient
from commutecompass.config import Config
from commutecompass.ha_client import push_tomorrow_alarm
from commutecompass.planner import plan_event
from commutecompass.store import Store
from commutecompass.timeutil import logical_day_bounds_nyc, now_nyc
from commutecompass.venues import VenueRegistry

if TYPE_CHECKING:
    from commutecompass.llm import OpencodeGoClient

from commutecompass.models import (
    CalendarSpec,
    Event,
    Plan,
    ZoneInfo,
)

logger = logging.getLogger(__name__)


def run(config: Config, *, dry_run: bool = False) -> Optional[Plan]:
    """Compute tomorrow's earliest prep_at and push it to HA.

    Returns the chosen Plan (so the CLI can echo it) or None when nothing
    was sent: no plan-able events tomorrow, or all plans errored.

    ``dry_run`` skips the HA POST but still computes and logs the choice.
    """
    _now = now_nyc()
    tomorrow_ref = _now + timedelta(days=1)
    day_start, day_end = logical_day_bounds_nyc(tomorrow_ref)

    calendar_client = CalendarClient(
        client_secret_json=config.google_oauth_client_secret_json,
        token_path=Path(config.paths.oauth_token_path),
    )
    events: list[Event] = []
    try:
        events = calendar_client.fetch_events(
            calendars=[
                CalendarSpec(id=cal.id, name=cal.name, enabled=cal.enabled)
                for cal in config.calendars
            ],
            start=day_start,
            end=day_end,
        )
    except Exception as exc:
        logger.error("tomorrow job: failed to fetch calendar events: %s", exc)
        return None

    logger.info("tomorrow job: fetched %d events for %s", len(events), day_start.date())

    if not events:
        logger.info("tomorrow job: no events tomorrow — nothing to push")
        return None

    store = Store(config.paths.db_path)
    store.init_schema()

    ha_zones: dict[str, ZoneInfo] = {}
    if config.home_assistant.enabled:
        from commutecompass.ha_client import fetch_zones as _ha_fetch_zones

        try:
            ha_zones = _ha_fetch_zones(
                config.home_assistant.base_url,
                config.home_assistant_token,
            )
        except Exception as exc:
            logger.warning("HA fetch_zones raised in tomorrow: %s", exc)
            ha_zones = {}

    venue_registry = VenueRegistry.load(Path(config.paths.venues_file))

    from commutecompass.llm import OpencodeGoClient
    llm_client = OpencodeGoClient(
        endpoint=config.opencode_go.endpoint,
        token=config.opencode_go_token,
        model=config.opencode_go.model,
    )

    plans: list[Plan] = []
    for event in events:
        plans.append(_plan_event_safe(event, config, venue_registry, store, llm_client, ha_zones))

    earliest = _earliest_prep(plans)
    if earliest is None:
        logger.info(
            "tomorrow job: no plan-able commutes tomorrow (events=%d) — nothing to push",
            len(events),
        )
        return None

    assert earliest.prep_at is not None
    logger.info(
        "tomorrow job: earliest prep_at=%s event=%s (%s)",
        earliest.prep_at.isoformat(),
        earliest.event.id,
        earliest.event.title,
    )

    if dry_run:
        logger.info("tomorrow job: dry_run — skipping HA push")
        return earliest

    if not config.home_assistant.tomorrow.enabled:
        logger.info("tomorrow job: [home_assistant.tomorrow] disabled — skipping HA push")
        return earliest

    if not (config.home_assistant.base_url and config.home_assistant_token):
        logger.warning(
            "tomorrow job: HA base_url or HOME_ASSISTANT_TOKEN missing — skipping HA push"
        )
        return earliest

    ok = push_tomorrow_alarm(
        config.home_assistant.base_url,
        config.home_assistant_token,
        config.home_assistant.tomorrow.script,
        earliest.prep_at,
        extra_data=config.home_assistant.tomorrow.extra_data,
    )
    if ok:
        logger.info(
            "tomorrow job: pushed alarm %s to HA via %s",
            earliest.prep_at.isoformat(),
            config.home_assistant.tomorrow.script,
        )
    else:
        logger.warning(
            "tomorrow job: HA push failed for %s",
            config.home_assistant.tomorrow.script,
        )
    return earliest


def _earliest_prep(plans: list[Plan]) -> Optional[Plan]:
    """Return the plan with the smallest prep_at, ignoring errored/empty plans."""
    from datetime import datetime as _dt

    plannable = [p for p in plans if p.prep_at is not None and p.error is None]
    if not plannable:
        return None

    def _key(p: Plan) -> _dt:
        assert p.prep_at is not None  # narrowed above
        return p.prep_at

    return min(plannable, key=_key)


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
            "tomorrow: plan_event failed for event %s (%s): %s",
            event.id,
            event.title,
            exc,
        )
        return Plan(event=event, error=f"internal_error: {exc}")
