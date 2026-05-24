"""Tests for planner.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

from commutecompass.config import (
    Config,
    CalendarSpec,
    HomeAssistantConfig,
    LocationOverride,
    MtaConfig,
    OpencodeGoConfig,
    Origin,
    PathsConfig,
    PrepConfig,
    SchedulingConfig,
    ZoneOrigin,
)
from commutecompass.models import (
    CurrentLocation,
    Event,
    ResolvedLocation,
    Route,
    TransitLeg,
)
from commutecompass.planner import effective_origin, plan_event, get_effective_location
from commutecompass.venues import VenueRegistry
from commutecompass.llm import OpencodeGoClient
from commutecompass.timeutil import NYC_TZ


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def nyc_now() -> datetime:
    return datetime(2026, 5, 8, 10, 0, 0, tzinfo=NYC_TZ)


@pytest.fixture
def origin() -> Origin:
    return Origin(
        address="123 Example Ave, Brooklyn NY 11201",
        lat=40.6950,
        lon=-73.9890,
        subway_station="Jay St-MetroTech",
        lirr_station="Atlantic Terminal",
    )


@pytest.fixture
def prep_config() -> PrepConfig:
    return PrepConfig(prep_minutes=20, safety_buffer_minutes=5)


@pytest.fixture
def config(origin: Origin, prep_config: PrepConfig) -> Config:
    return Config(
        origin=origin,
        calendars=[
            CalendarSpec(id="test-cal", name="Test", enabled=True),
        ],
        prep=prep_config,
        scheduling=SchedulingConfig(),
        paths=PathsConfig(
            venues_file="/tmp/venues.yaml",
            db_path="/tmp/test.db",
            oauth_token_path="/tmp/token.json",
        ),
        opencode_go=OpencodeGoConfig(
            endpoint="https://example.com/chat",
            model="test-model",
        ),
        mta=MtaConfig(
            subway_alerts_url="https://example.com/subway",
            lirr_alerts_url="https://example.com/lirr",
            bus_alerts_url="https://example.com/bus",
        ),
        google_maps_api_key="test-api-key",
    )


@pytest.fixture
def event(nyc_now: datetime) -> Event:
    return Event(
        id="evt-1",
        calendar_id="test-cal",
        calendar_name="Test",
        title="Example Class",
        start=nyc_now.replace(hour=14, minute=30),
        end=nyc_now.replace(hour=16, minute=0),
        location_raw="200 Example St, New York, NY 10001",
    )


@pytest.fixture
def resolved_location() -> ResolvedLocation:
    return ResolvedLocation(
        kind="address",
        value="200 Example St, New York, NY 10001",
        lat=40.7128,
        lon=-74.0060,
        source="known_venues",
    )


@pytest.fixture
def mock_route(nyc_now: datetime) -> Route:
    depart = nyc_now.replace(hour=13, minute=45)
    arrive = nyc_now.replace(hour=14, minute=30)
    return Route(
        legs=[
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=depart,
                arrive_at=depart + timedelta(minutes=5),
                duration_seconds=300,
                summary="Walk to station",
            ),
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St",
                depart_at=depart + timedelta(minutes=5),
                arrive_at=arrive - timedelta(minutes=5),
                duration_seconds=2700,
                summary="C train from Jay St-MetroTech to Fulton St",
            ),
            TransitLeg(
                mode="WALKING",
                system=None,
                line=None,
                headsign=None,
                depart_at=arrive - timedelta(minutes=5),
                arrive_at=arrive,
                duration_seconds=300,
                summary="Walk to venue",
            ),
        ],
        depart_at=depart,
        arrive_at=arrive,
        total_duration_seconds=3300,  # 55 minutes
        transfers=0,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_plan_event_resolves_location_and_computes_timings(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
    nyc_now: datetime,
) -> None:
    """plan_event wires resolver + routing, computes leave_at/prep_at correctly."""
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    assert result.error is None
    assert result.route is mock_route
    # event.start = 14:30, travel = 55min (3300s), safety = 5min, prep = 20min
    # leave_at = 14:30 - 55min - 5min = 13:30
    # prep_at  = 13:30 - 20min = 13:10
    assert result.leave_at == nyc_now.replace(hour=13, minute=30, second=0, microsecond=0)
    assert result.prep_at == nyc_now.replace(hour=13, minute=10, second=0, microsecond=0)
    assert result.event.location_resolved is resolved_location


def test_plan_event_location_unresolved(
    event: Event,
    config: Config,
    nyc_now: datetime,
) -> None:
    """Returns error='location_unresolved' when resolve returns None."""
    with patch("commutecompass.resolver.resolve") as mock_resolve:
        mock_resolve.return_value = None

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    assert result.error == "location_unresolved"
    assert result.route is None
    assert result.leave_at is None
    assert result.prep_at is None


def test_plan_event_no_route(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    nyc_now: datetime,
) -> None:
    """Returns error='no_route' when plan_route returns None."""
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = None

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    assert result.error == "no_route"
    assert result.route is None
    assert result.leave_at is None
    assert result.prep_at is None


def test_plan_event_mode_override(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
    nyc_now: datetime,
) -> None:
    """mode_override parameter is passed through to plan_route."""
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
            mode_override="driving",
        )

    # Verify driving was passed to plan_route
    _, kwargs = mock_plan_route.call_args
    assert kwargs["mode"] == "driving"
    assert result.error is None


def test_plan_event_uses_event_mode_override(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
) -> None:
    """Falls back to event.mode_override when no explicit override provided."""
    event.mode_override = "walking"

    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    _, kwargs = mock_plan_route.call_args
    assert kwargs["mode"] == "walking"


def test_plan_event_updates_event_with_resolved_location(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
) -> None:
    """Returned Plan has event with location_resolved populated."""
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    assert result.event.location_resolved is resolved_location
    assert result.event.location_resolved.kind == "address"


def test_plan_event_timezone_aware_arithmetic(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    nyc_now: datetime,
) -> None:
    """Timings are timezone-aware (preserve tzinfo on subtraction)."""
    # Use a route with known duration: 1 hour = 3600s
    one_hour_route = Route(
        legs=[
            TransitLeg(
                mode="TRANSIT",
                system="MTA Subway",
                line="C",
                headsign="Fulton St",
                depart_at=nyc_now.replace(hour=13, minute=0),
                arrive_at=nyc_now.replace(hour=14, minute=0),
                duration_seconds=3600,
                summary="C train",
            ),
        ],
        depart_at=nyc_now.replace(hour=13, minute=0),
        arrive_at=nyc_now.replace(hour=14, minute=0),
        total_duration_seconds=3600,
        transfers=0,
    )

    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = one_hour_route

        result = plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    assert result.leave_at is not None
    assert result.prep_at is not None
    assert result.leave_at.tzinfo is not None
    assert result.prep_at.tzinfo is not None
    assert result.leave_at.tzinfo == NYC_TZ
    assert result.prep_at.tzinfo == NYC_TZ


def test_plan_event_default_mode_is_transit(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
) -> None:
    """When neither override nor event mode is set, defaults to transit."""
    assert event.mode_override is None

    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    _, kwargs = mock_plan_route.call_args
    assert kwargs["mode"] == "transit"


# ── Location Override tests ─────────────────────────────────────────────────────

def test_get_effective_location_no_overrides(event: Event, config: Config) -> None:
    """When no overrides exist, returns event.location_raw."""
    assert config.location_overrides == []
    result = get_effective_location(event, config)
    assert result == event.location_raw


def test_get_effective_location_calendar_only_match(event: Event, config: Config) -> None:
    """Override applies when calendar_id matches and no title_contains set."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == "200 Example St, New York, NY 10001"


def test_get_effective_location_title_contains_match(event: Event, config: Config) -> None:
    """Override applies when calendar_id matches AND title contains substring."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            title_contains="Example",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == "200 Example St, New York, NY 10001"


def test_get_effective_location_title_contains_case_insensitive(event: Event, config: Config) -> None:
    """title_contains match is case-insensitive."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            title_contains="example class",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == "200 Example St, New York, NY 10001"


def test_get_effective_location_no_override_calendar_mismatch(event: Event, config: Config) -> None:
    """No override when calendar_id does not match."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="other-cal",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == event.location_raw


def test_get_effective_location_no_override_title_mismatch(event: Event, config: Config) -> None:
    """No override when title_contains is set but title does not contain it."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            title_contains="Yoga",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == event.location_raw


def test_get_effective_location_first_matching_override_wins(event: Event, config: Config) -> None:
    """When multiple overrides match, the first one in the list applies."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            title_contains="Example",
            location="First Match",
        ),
        LocationOverride(
            calendar_id="test-cal",
            location="Second Match",
        ),
    ]
    result = get_effective_location(event, config)
    assert result == "First Match"


def test_plan_event_uses_location_override(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
) -> None:
    """plan_event passes override location to resolver when match is found."""
    config.location_overrides = [
        LocationOverride(
            calendar_id="test-cal",
            title_contains="Example",
            location="200 Example St, New York, NY 10001",
        ),
    ]
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    mock_resolve.assert_called_once()
    call_args = mock_resolve.call_args
    assert call_args[0][0] == "200 Example St, New York, NY 10001"


def test_plan_event_no_override_uses_event_location(
    event: Event,
    config: Config,
    resolved_location: ResolvedLocation,
    mock_route: Route,
) -> None:
    """Without a matching override, plan_event uses event.location_raw."""
    assert config.location_overrides == []
    with patch("commutecompass.resolver.resolve") as mock_resolve, \
         patch("commutecompass.routing.plan_route") as mock_plan_route:
        mock_resolve.return_value = resolved_location
        mock_plan_route.return_value = mock_route

        plan_event(
            event,
            config,
            MagicMock(spec=VenueRegistry),
            MagicMock(),
            MagicMock(spec=OpencodeGoClient),
        )

    mock_resolve.assert_called_once()
    call_args = mock_resolve.call_args
    assert call_args[0][0] == event.location_raw


# ── effective_origin tests ────────────────────────────────────────────────────


def _ha_enabled_config(base: Config) -> Config:
    return base.model_copy(
        update={
            "home_assistant": HomeAssistantConfig(
                enabled=True,
                base_url="http://ha",
                entity_id="device_tracker.iphone",
                home_zone="home",
                max_age_minutes=30,
            )
        }
    )


def test_effective_origin_returns_explicit_override(config: Config) -> None:
    from commutecompass.models import Origin as ModelOrigin

    override = ModelOrigin(address="ovr", lat=1.0, lon=2.0)
    store = MagicMock()
    result = effective_origin(config, store, override=override)
    assert result is override
    store.get_current_location.assert_not_called()


def test_effective_origin_returns_config_when_ha_disabled(config: Config) -> None:
    store = MagicMock()
    result = effective_origin(config, store)
    assert result.lat == config.origin.lat
    assert result.lon == config.origin.lon
    store.get_current_location.assert_not_called()


def test_effective_origin_returns_config_when_no_current_location(config: Config) -> None:
    cfg = _ha_enabled_config(config)
    store = MagicMock()
    store.get_current_location.return_value = None
    result = effective_origin(cfg, store)
    assert result.lat == cfg.origin.lat
    assert result.subway_station == cfg.origin.subway_station


def test_effective_origin_returns_config_when_zone_is_home(config: Config) -> None:
    from commutecompass.timeutil import now_nyc

    cfg = _ha_enabled_config(config)
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=1.0, lon=2.0, zone="home", captured_at=now_nyc()
    )
    result = effective_origin(cfg, store)
    assert result.lat == cfg.origin.lat
    assert result.subway_station == cfg.origin.subway_station


def test_effective_origin_uses_live_coords_when_away(config: Config) -> None:
    from commutecompass.timeutil import now_nyc

    cfg = _ha_enabled_config(config)
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=40.7128, lon=-74.006, zone="not_home", captured_at=now_nyc()
    )
    result = effective_origin(cfg, store)
    assert result.lat == 40.7128
    assert result.lon == -74.006
    # Built without station hints
    assert result.subway_station == ""
    assert result.lirr_station == ""


def test_effective_origin_home_match_is_case_insensitive(config: Config) -> None:
    """HA returns the zone friendly_name (e.g. "Home"); config uses slug ("home")."""
    from commutecompass.timeutil import now_nyc

    cfg = _ha_enabled_config(config)
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=1.0, lon=2.0, zone="Home", captured_at=now_nyc()
    )
    result = effective_origin(cfg, store)
    assert result.lat == cfg.origin.lat
    assert result.subway_station == cfg.origin.subway_station


def test_effective_origin_returns_zone_origin_when_zone_matches(config: Config) -> None:
    from commutecompass.timeutil import now_nyc

    cfg = config.model_copy(
        update={
            "home_assistant": HomeAssistantConfig(
                enabled=True,
                base_url="http://ha",
                entity_id="person.finn",
                home_zone="home",
                max_age_minutes=30,
                zone_origins=[
                    ZoneOrigin(
                        zone="Work",
                        address="200 W Street, NY",
                        lat=40.7346,
                        lon=-74.0055,
                        subway_station="34 St-Penn Station",
                    ),
                ],
            )
        }
    )
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=40.7346, lon=-74.0055, zone="Work", captured_at=now_nyc()
    )
    result = effective_origin(cfg, store)
    assert result.address == "200 W Street, NY"
    assert result.lat == 40.7346
    assert result.subway_station == "34 St-Penn Station"


def test_effective_origin_zone_origin_match_is_case_insensitive(config: Config) -> None:
    from commutecompass.timeutil import now_nyc

    cfg = config.model_copy(
        update={
            "home_assistant": HomeAssistantConfig(
                enabled=True,
                base_url="http://ha",
                entity_id="person.finn",
                zone_origins=[
                    ZoneOrigin(
                        zone="cap21",
                        address="18 Bridge St, NY",
                        lat=40.7062,
                        lon=-74.0124,
                    ),
                ],
            )
        }
    )
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=40.7062, lon=-74.0124, zone="CAP21", captured_at=now_nyc()
    )
    result = effective_origin(cfg, store)
    assert result.address == "18 Bridge St, NY"


def test_effective_origin_rejects_low_accuracy_fix(config: Config) -> None:
    from commutecompass.timeutil import now_nyc

    cfg = config.model_copy(
        update={
            "home_assistant": HomeAssistantConfig(
                enabled=True,
                base_url="http://ha",
                entity_id="person.finn",
                home_zone="home",
                max_age_minutes=30,
                min_gps_accuracy_meters=200,
            )
        }
    )
    store = MagicMock()
    store.get_current_location.return_value = CurrentLocation(
        lat=40.7128,
        lon=-74.006,
        zone="not_home",
        captured_at=now_nyc(),
        accuracy_m=1500.0,
    )
    result = effective_origin(cfg, store)
    # Bad fix → fall back to config.origin (with station hints)
    assert result.lat == cfg.origin.lat
    assert result.subway_station == cfg.origin.subway_station
