"""Tests for models.py."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from commutecompass.models import (
    Origin,
    CalendarSpec,
    PrepConfig,
    SchedulingConfig,
    PathsConfig,
    OpencodeGoConfig,
    MtaConfig,
    Config,
    ResolvedLocation,
    Event,
    TransitLeg,
    Route,
    Plan,
    Alert,
    PingEntry,
)


# ─────────── Timezone helpers ───────────

def nyc_now() -> datetime:
    """Current time in America/New_York (naive offset for naive datetime tests)."""
    return datetime.now(timezone(timedelta(hours=-5)))  # EST, not handling DST here


def aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (America/New_York)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone(timedelta(hours=-5)))
    return dt


# ─────────── Config model tests ───────────

def test_origin_required_fields() -> None:
    origin = Origin(
        address="123 Example Ave, Brooklyn NY 11201",
        lat=40.6950,
        lon=-73.9890,
        subway_station="Jay St-MetroTech",
        lirr_station="Atlantic Terminal",
    )
    assert origin.address == "123 Example Ave, Brooklyn NY 11201"
    assert origin.lat == 40.6950
    assert origin.lon == -73.9890


def test_calendar_spec_defaults() -> None:
    cal = CalendarSpec(id="abc", name="Test Calendar")
    assert cal.enabled is True


def test_prep_config_defaults() -> None:
    prep = PrepConfig()
    assert prep.prep_minutes == 20
    assert prep.safety_buffer_minutes == 5


def test_scheduling_config_defaults() -> None:
    from datetime import time

    sched = SchedulingConfig()
    assert sched.morning_run_time == time(6, 0)
    assert sched.poll_interval_seconds == 60
    assert sched.quiet_hours_start is None
    assert sched.quiet_hours_end is None


def test_opencode_go_config_defaults() -> None:
    cfg = OpencodeGoConfig(endpoint="https://example.com/v1/chat/completions")
    assert cfg.model == "deepseek-v4-flash"


def test_config_full_construction() -> None:
    config = Config(
        origin=Origin(
            address="123 Example Ave",
            lat=40.6950,
            lon=-73.9890,
            subway_station="Jay St-MetroTech",
            lirr_station="Atlantic Terminal",
        ),
        calendars=[
            CalendarSpec(id="cal-1", name="Theatre"),
            CalendarSpec(id="cal-2", name="School", enabled=False),
        ],
        prep=PrepConfig(prep_minutes=25),
        scheduling=SchedulingConfig(morning_run_time="06:30"),
        paths=PathsConfig(
            venues_file="/etc/commutecompass/venues.yaml",
            db_path="/var/lib/commutecompass/state.db",
            oauth_token_path="/var/lib/commutecompass/token.json",
        ),
        opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
        mta=MtaConfig(
            subway_alerts_url="https://example.com/subway.pb",
            lirr_alerts_url="https://example.com/lirr.pb",
            bus_alerts_url="https://example.com/bus.pb",
        ),
        google_maps_api_key="AIzaSecret",
        google_oauth_client_secret_json='{"installed":{"client_id":"x"}}',
        telegram_bot_token="123456:ABC",
        telegram_chat_id=987654321,
        opencode_go_token="sk-secret",
    )
    assert config.google_maps_api_key == "AIzaSecret"
    assert len(config.calendars) == 2
    assert config.calendars[1].enabled is False


# ─────────── Domain model tests ───────────

def test_resolved_location_address() -> None:
    loc = ResolvedLocation(
        kind="address",
        value="200 Example St, New York, NY 10001",
        lat=40.7128,
        lon=-74.0060,
        source="geocode",
    )
    assert loc.kind == "address"
    assert loc.lat == 40.7128


def test_resolved_location_station() -> None:
    loc = ResolvedLocation(
        kind="station",
        value="Example LIRR Station, NY",
        source="llm",
    )
    assert loc.kind == "station"
    assert loc.lat is None


def test_event_with_aware_datetimes() -> None:
    start = aware(datetime(2025, 5, 12, 9, 30))
    end = aware(datetime(2025, 5, 12, 11, 0))
    event = Event(
        id="evt-001",
        calendar_id="theatre-calendar@example.com",
        calendar_name="Theatre",
        title="Example Class — Example School",
        start=start,
        end=end,
        location_raw="200 Example St",
        mode_override="transit",
    )
    assert event.start.tzinfo is not None
    assert event.location_raw == "200 Example St"
    assert event.mode_override == "transit"


def test_event_minimal() -> None:
    start = aware(datetime(2025, 5, 12, 14, 0))
    end = aware(datetime(2025, 5, 12, 15, 0))
    event = Event(
        id="evt-002",
        calendar_id="cal",
        calendar_name="School",
        title="ASL",
        start=start,
        end=end,
    )
    assert event.location_raw is None
    assert event.location_resolved is None
    assert event.mode_override is None


def test_event_with_resolved_location() -> None:
    start = aware(datetime(2025, 5, 12, 14, 0))
    end = aware(datetime(2025, 5, 12, 15, 0))
    resolved = ResolvedLocation(
        kind="station",
        value="Example LIRR Station, NY",
        lat=40.6588,
        lon=-73.6337,
        source="llm",
    )
    event = Event(
        id="evt-003",
        calendar_id="cal",
        calendar_name="School",
        title="Example University",
        start=start,
        end=end,
        location_raw="Example University",
        location_resolved=resolved,
    )
    assert event.location_resolved is not None
    assert event.location_resolved.kind == "station"


def test_transit_leg_defaults() -> None:
    depart = aware(datetime(2025, 5, 12, 7, 45))
    arrive = aware(datetime(2025, 5, 12, 8, 5))
    leg = TransitLeg(
        mode="TRANSIT",
        depart_at=depart,
        arrive_at=arrive,
        duration_seconds=1200,
        summary="C train to Fulton St",
    )
    assert leg.system is None
    assert leg.line is None
    assert leg.headsign is None


def test_transit_leg_full() -> None:
    depart = aware(datetime(2025, 5, 12, 7, 45))
    arrive = aware(datetime(2025, 5, 12, 8, 5))
    leg = TransitLeg(
        mode="TRANSIT",
        system="MTA Subway",
        line="C",
        headsign="Fulton St",
        depart_at=depart,
        arrive_at=arrive,
        duration_seconds=1200,
        summary="C train from Jay St-MetroTech to Fulton St",
    )
    assert leg.system == "MTA Subway"
    assert leg.line == "C"


def test_route_full() -> None:
    depart = aware(datetime(2025, 5, 12, 7, 30))
    arrive = aware(datetime(2025, 5, 12, 9, 15))
    leg1 = TransitLeg(
        mode="WALKING",
        depart_at=depart,
        arrive_at=aware(datetime(2025, 5, 12, 7, 45)),
        duration_seconds=900,
        summary="Walk to Jay St-MetroTech station",
    )
    leg2 = TransitLeg(
        mode="TRANSIT",
        system="MTA Subway",
        line="C",
        depart_at=aware(datetime(2025, 5, 12, 7, 45)),
        arrive_at=arrive,
        duration_seconds=3600,
        summary="C train to Fulton St",
    )
    route = Route(
        legs=[leg1, leg2],
        depart_at=depart,
        arrive_at=arrive,
        total_duration_seconds=4500,
        transfers=0,
        fare_estimate_cents=275,
        raw_provider_payload={"provider": "google"},
    )
    assert len(route.legs) == 2
    assert route.transfers == 0
    assert route.fare_estimate_cents == 275


def test_route_defaults() -> None:
    now = aware(datetime.now())
    route = Route(
        legs=[],
        depart_at=now,
        arrive_at=now,
        total_duration_seconds=0,
    )
    assert route.transfers == 0
    assert route.fare_estimate_cents is None
    assert route.raw_provider_payload is None


def test_plan_full() -> None:
    start = aware(datetime(2025, 5, 12, 9, 30))
    end = aware(datetime(2025, 5, 12, 11, 0))
    event = Event(
        id="evt-001",
        calendar_id="cal",
        calendar_name="Theatre",
        title="Example Class",
        start=start,
        end=end,
    )
    leave_at = aware(datetime(2025, 5, 12, 7, 55))
    prep_at = aware(datetime(2025, 5, 12, 7, 35))
    plan = Plan(
        event=event,
        leave_at=leave_at,
        prep_at=prep_at,
    )
    assert plan.route is None
    assert plan.error is None


def test_plan_with_error() -> None:
    start = aware(datetime(2025, 5, 12, 9, 30))
    end = aware(datetime(2025, 5, 12, 11, 0))
    event = Event(
        id="evt-err",
        calendar_id="cal",
        calendar_name="Test",
        title="Unresolvable",
        start=start,
        end=end,
        location_raw="Somewhere no go",
    )
    plan = Plan(event=event, error="location_unresolved")
    assert plan.error == "location_unresolved"
    assert plan.leave_at is None


def test_alert_defaults() -> None:
    alert = Alert(
        id="alert-001",
        header="C train delays",
        description="Expect minor delays on the C line due to signal issues.",
    )
    assert alert.severity == "INFO"
    assert alert.affected_routes == set()
    assert alert.affected_systems == set()
    assert alert.active_periods == []
    assert alert.url is None


def test_alert_full() -> None:
    alert = Alert(
        id="alert-002",
        header="Weekend track work",
        description="B44 and Bx41 routes affected by track work.",
        affected_routes={"C", "B44"},
        affected_systems={"MTA Subway", "MTA Bus"},
        active_periods=[
            (aware(datetime(2025, 5, 10, 0, 0)), aware(datetime(2025, 5, 12, 23, 59))),
        ],
        severity="WARNING",
        url="https://example.com/alerts/123",
    )
    assert alert.severity == "WARNING"
    assert "C" in alert.affected_routes
    assert alert.active_periods[0][0] < alert.active_periods[0][1]


def test_ping_entry_defaults() -> None:
    fire = aware(datetime(2025, 5, 12, 7, 35))
    ping = PingEntry(
        id="ping-001",
        event_id="evt-001",
        kind="prep",
        fire_at=fire,
        message="Time to get ready for Example Class",
    )
    assert ping.fired is False
    assert ping.fired_at is None


def test_ping_entry_fired() -> None:
    fire = aware(datetime(2025, 5, 12, 7, 35))
    fired_at = aware(datetime(2025, 5, 12, 7, 36))
    ping = PingEntry(
        id="ping-002",
        event_id="evt-001",
        kind="leave",
        fire_at=fire,
        fired=True,
        fired_at=fired_at,
        message="Leave now for Example Class",
    )
    assert ping.fired is True
    assert ping.fired_at is not None


def test_ping_entry_all_kinds() -> None:
    fire = aware(datetime.now())
    for kind in ("digest", "prep", "leave", "service_update"):
        ping = PingEntry(
            id=f"ping-{kind}",
            event_id="evt-001",
            kind=kind,  # type: ignore[arg-type]
            fire_at=fire,
            message=f"Test {kind}",
        )
        assert ping.kind == kind


def test_event_mode_override_literals() -> None:
    start = aware(datetime.now())
    end = aware(datetime.now())
    for mode in ("transit", "driving", "walking", "bicycling"):
        event = Event(
            id="x",
            calendar_id="c",
            calendar_name="n",
            title="t",
            start=start,
            end=end,
            mode_override=mode,  # type: ignore[arg-type]
        )
        assert event.mode_override == mode


def test_transit_leg_mode_literals() -> None:
    now = aware(datetime.now())
    for mode in ("WALKING", "TRANSIT", "DRIVING", "BICYCLING"):
        leg = TransitLeg(
            mode=mode,  # type: ignore[arg-type]
            depart_at=now,
            arrive_at=now,
            duration_seconds=60,
            summary="test",
        )
        assert leg.mode == mode


def test_alert_severity_literals() -> None:
    for sev in ("INFO", "WARNING", "SEVERE"):
        alert = Alert(
            id="x",
            header="h",
            description="d",
            severity=sev,  # type: ignore[arg-type]
        )
        assert alert.severity == sev