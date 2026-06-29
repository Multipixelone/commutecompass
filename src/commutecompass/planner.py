"""Plan-an-event orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Literal, Optional

from commutecompass.config import Config
from commutecompass.geocode import GeocodeResult
from commutecompass.models import Event, Origin, Plan, ZoneInfo
from commutecompass.venues import VenueRegistry
from commutecompass.llm import OpencodeGoClient
from commutecompass.timeutil import now_nyc

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


def get_effective_mode(
    effective_location: str,
    config: Config,
) -> Optional[Literal["transit", "driving", "walking", "bicycling"]]:
    """Return the forced travel mode if the location matches a mode_override.

    ``location_contains`` is matched case-insensitively as a substring against
    the (effective) location. First matching rule wins. Returns None when no
    rule matches so callers can fall back to their default.
    """
    hay = (effective_location or "").lower()
    for ov in config.mode_overrides:
        if ov.location_contains.lower() in hay:
            return ov.mode
    return None


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
    from commutecompass.routing import estimate_route, plan_route, route_cache_key
    from commutecompass.geocode import geocode

    # Step 1: resolve location (override applied first)
    raw_location = get_effective_location(event, config)

    # Determine travel mode (CLI > event > location-based config > default)
    mode: Literal["transit", "driving", "walking", "bicycling"] = (
        mode_override
        or event.mode_override
        or get_effective_mode(raw_location, config)
        or "transit"
    )

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

    # Step 2: plan route.  A live route is cached for reuse; if live routing is
    # unavailable we fall back to the last good cached route, then to a coarse
    # distance estimate — so an API outage degrades to "approximate" rather than
    # silently producing no plan (and therefore no alarm) for the whole day.
    route_origin = effective_origin(config, store, override=origin_override)
    cache_key = route_cache_key(route_origin)

    route = plan_route(
        origin=route_origin,
        destination=resolved,
        arrival_time=event.start,
        mode=mode,
        api_key=config.google_maps_api_key,
    )
    if route is not None:
        store.cache_route(cache_key, resolved.value, mode, route)
    else:
        route = store.get_cached_route(cache_key, resolved.value, mode)
        if route is not None:
            route = route.model_copy(update={"approximate": True})
        else:
            route = estimate_route(route_origin, resolved, event.start, mode)
    if route is None:
        return Plan(event=event, error="no_route")

    # Step 3: compute timings.  Add a weather buffer when precipitation is
    # expected around the event so the alarm fires earlier on a rainy/snowy day.
    from commutecompass.weather import weather_buffer as _weather_buffer
    from commutecompass.realtime import realtime_delay as _realtime_delay

    wx = _weather_buffer(route_origin.lat, route_origin.lon, event.start, config.weather)
    # Real-time delay on the boarding line — only ever adds buffer (leave earlier),
    # never moves the leave time later.  Fail-open: zero on any error.
    rt = _realtime_delay(route, event.start, config.realtime)

    travel = timedelta(seconds=route.total_duration_seconds)
    buffer = timedelta(minutes=config.prep.safety_buffer_minutes + wx.minutes + rt.minutes)
    prep = timedelta(minutes=config.prep.prep_minutes)

    leave_at = event.start - travel - buffer
    prep_at = leave_at - prep

    # Too-imminent guard: if leave_at is already in the past, the event was
    # added to the calendar after the user would have needed to depart.
    # Emit a structured error so the digest / chat surface can tell the user
    # rather than silently storing a Plan with past times that will never fire.
    if leave_at < now_nyc():
        return Plan(
            event=event.model_copy(update={"location_resolved": resolved}),
            route=route,
            leave_at=leave_at,
            prep_at=prep_at,
            error="too_imminent",
            weather_buffer_minutes=wx.minutes,
            weather_reason=wx.reason,
            realtime_buffer_minutes=rt.minutes,
            realtime_reason=rt.reason,
        )

    return Plan(
        event=event.model_copy(update={"location_resolved": resolved}),
        route=route,
        leave_at=leave_at,
        prep_at=prep_at,
        weather_buffer_minutes=wx.minutes,
        weather_reason=wx.reason,
        realtime_buffer_minutes=rt.minutes,
        realtime_reason=rt.reason,
    )
