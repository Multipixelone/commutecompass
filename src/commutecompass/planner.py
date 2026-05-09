"""Plan-an-event orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from commutecompass.models import Config, Event, Plan
from commutecompass.venues import VenueRegistry
from commutecompass.llm import OpencodeGoClient


def get_effective_location(
    event: Event,
    config: Config,
) -> str:
    """Apply location overrides if event matches calendar_id and title_contains."""
    for ov in config.location_overrides:
        if ov.calendar_id != event.calendar_id:
            continue
        if ov.title_contains is None:
            return ov.location
        if ov.title_contains.lower() in event.title.lower():
            return ov.location
    return event.location_raw or ""


def plan_event(
    event: Event,
    config: Config,
    venues: VenueRegistry,
    store: "Store",  # type: ignore[name-defined]  # noqa: F821
    llm: OpencodeGoClient,
    *,
    mode_override: Optional[str] = None,
) -> Plan:
    """Compute optimal departure time for an event.

    Algorithm (§6.11):
    1. Resolve location via resolver.resolve pipeline.
    2. Plan route via routing.plan_route.
    3. Compute leave_at = event.start - travel - safety_buffer.
    4. Compute prep_at  = leave_at - prep_minutes.

    Returns a Plan with route and timing, or an error Plan on failure.
    """
    from commutecompass.resolver import resolve
    from commutecompass.routing import plan_route
    from commutecompass.geocode import geocode

    # Determine travel mode
    mode: str = mode_override or event.mode_override or "transit"

    # Step 1: resolve location (override applied first)
    raw_location = get_effective_location(event, config)
    resolved = resolve(
        raw_location,
        venues=venues,
        store=store,
        geocoder=geocode,
        llm=llm,
    )
    if resolved is None:
        return Plan(event=event, error="location_unresolved")

    # Step 2: plan route
    route = plan_route(
        origin=config.origin,
        destination=resolved,
        arrival_time=event.start,
        mode=mode,  # type: ignore[arg-type]
        api_key=config.google_maps_api_key,
    )
    if route is None:
        return Plan(event=event, error="no_route")

    # Step 3: compute timings
    travel = timedelta(seconds=route.total_duration_seconds)
    buffer = timedelta(minutes=config.prep.safety_buffer_minutes)
    prep = timedelta(minutes=config.prep.prep_minutes)

    leave_at = event.start - travel - buffer
    prep_at = leave_at - prep

    return Plan(
        event=event.model_copy(update={"location_resolved": resolved}),
        route=route,
        leave_at=leave_at,
        prep_at=prep_at,
    )
