"""Plan-an-event orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Literal, Optional

from commutecompass.config import Config
from commutecompass.geocode import GeocodeResult
from commutecompass.models import Event, Origin, Plan, ZoneInfo
from commutecompass.venues import VenueRegistry
from commutecompass.llm import OpencodeGoClient

if TYPE_CHECKING:
    from commutecompass.store import Store


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


def effective_origin(
    config: Config,
    store: "Store",
    *,
    override: Optional[Origin] = None,
) -> Origin:
    """Pick the origin to plan from.

    Precedence:
      1. explicit override (CLI --here, etc.)
      2. configured zone_origins[*] when the tracker is in that zone
      3. config.origin when the tracker is in the home zone (preserves hints)
      4. fresh HA-tracked GPS coords (no station hints)
      5. config.origin fallback
    """
    base_origin = Origin(
        address=config.origin.address,
        lat=config.origin.lat,
        lon=config.origin.lon,
        subway_station=config.origin.subway_station,
        lirr_station=config.origin.lirr_station,
    )

    if override is not None:
        return override

    if not config.home_assistant.enabled:
        return base_origin

    cl = store.get_current_location(max_age_minutes=config.home_assistant.max_age_minutes)
    if cl is None:
        return base_origin

    threshold_m = float(config.home_assistant.min_gps_accuracy_meters)
    if cl.accuracy_m is not None and threshold_m > 0 and cl.accuracy_m > threshold_m:
        return base_origin

    zone_lower = cl.zone.lower() if isinstance(cl.zone, str) else None

    if zone_lower is not None:
        for zo in config.home_assistant.zone_origins:
            if zo.zone.lower() == zone_lower:
                return Origin(
                    address=zo.address,
                    lat=zo.lat,
                    lon=zo.lon,
                    subway_station=zo.subway_station,
                    lirr_station=zo.lirr_station,
                )

        if zone_lower == config.home_assistant.home_zone.lower():
            return base_origin

    return Origin(
        address=f"{cl.lat:.6f},{cl.lon:.6f}",
        lat=cl.lat,
        lon=cl.lon,
    )


def plan_event(
    event: Event,
    config: Config,
    venues: VenueRegistry,
    store: "Store",
    llm: OpencodeGoClient,
    *,
    mode_override: Optional[Literal["transit", "driving", "walking", "bicycling"]] = None,
    origin_override: Optional[Origin] = None,
    ha_zones: Optional[dict[str, ZoneInfo]] = None,
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
    mode: Literal["transit", "driving", "walking", "bicycling"] = (
        mode_override or event.mode_override or "transit"
    )

    # Step 1: resolve location (override applied first)
    raw_location = get_effective_location(event, config)

    def geocoder(addr: str) -> Optional[GeocodeResult]:
        return geocode(addr, config.google_maps_api_key)

    resolved = resolve(
        raw_location,
        venues=venues,
        store=store,
        geocoder=geocoder,
        llm=llm,
        ha_zones=ha_zones,
    )
    if resolved is None:
        return Plan(event=event, error="location_unresolved")

    # Step 2: plan route
    route_origin = effective_origin(config, store, override=origin_override)

    route = plan_route(
        origin=route_origin,
        destination=resolved,
        arrival_time=event.start,
        mode=mode,
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
