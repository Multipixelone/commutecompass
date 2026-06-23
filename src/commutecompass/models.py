"""All pydantic / dataclass models."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ─────────── Configuration models ───────────

class Origin(BaseModel):
    address: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    subway_station: str = ""
    lirr_station: str = ""


class CalendarSpec(BaseModel):
    id: str
    name: str
    enabled: bool = True


class PrepConfig(BaseModel):
    prep_minutes: int = Field(default=20, ge=0, le=24 * 60)
    safety_buffer_minutes: int = Field(default=5, ge=0, le=24 * 60)


class SchedulingConfig(BaseModel):
    morning_run_time: time = time(6, 0)
    poll_interval_seconds: int = Field(default=60, ge=1, le=86_400)
    quiet_hours_start: Optional[time] = None
    quiet_hours_end: Optional[time] = None


class PathsConfig(BaseModel):
    venues_file: str
    db_path: str
    oauth_token_path: str


class OpencodeGoConfig(BaseModel):
    endpoint: str
    model: str = "deepseek-v4-flash"


class MtaConfig(BaseModel):
    subway_alerts_url: str
    lirr_alerts_url: str
    bus_alerts_url: str


class LocationOverride(BaseModel):
    calendar_id: str
    title_contains: Optional[str] = None
    location: str


class ModeOverride(BaseModel):
    """Force a travel mode for events whose (effective) location matches.

    ``location_contains`` is matched case-insensitively as a substring against
    the event's effective location (after ``location_overrides`` are applied).
    First matching rule wins. Handy for "always bike to work".
    """

    location_contains: str
    mode: Literal["transit", "driving", "walking", "bicycling"]


class ZoneOrigin(BaseModel):
    """Per-zone origin override with subway/LIRR hints.

    When the tracker reports being in a zone matching `zone` (case-insensitive,
    matched against the HA zone's friendly_name), planning uses this Origin
    instead of the fallback `[origin]` block. Lets non-home zones like "Work"
    or "CAP21" carry their own station hints.
    """

    zone: str
    address: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    subway_station: str = ""
    lirr_station: str = ""


class HomeAssistantAlarmConfig(BaseModel):
    """Optional additive alarm channel — fires an HA service when prep/leave pings fire.

    The intent is to delegate the "wake the user up loudly" mechanism to HA
    itself (Pushcut, looping critical notification, a HomePod media_player,
    a script that chains them — anything callable as an HA service).  This
    keeps CommuteCompass out of the iOS-alarm-clock business.

    Reuses ``HomeAssistantConfig.base_url`` and ``HOME_ASSISTANT_TOKEN``.
    """

    enabled: bool = False
    service: str = ""  # "domain.service", e.g. "script.commute_alarm"
    kinds: list[Literal["prep", "leave"]] = Field(
        default_factory=lambda: ["prep", "leave"]  # type: ignore[arg-type]
    )
    extra_data: dict[str, Any] = Field(default_factory=dict)


class HomeAssistantTomorrowConfig(BaseModel):
    """Pull-model alarm: push tomorrow's earliest prep_at into an HA helper.

    Designed for an iOS Shortcuts automation that polls HA each evening,
    reads the resulting state, and creates a wake alarm on-device. No HA
    automation is required on the receiving side beyond the script this
    block points at.

    ``script`` is the HA service to call as ``"domain.service"`` (typically
    ``script.commute_set_tomorrow_alarm``). The service receives a single
    ``datetime`` variable in ISO-8601 form (NYC-local with offset). It is
    expected to write that value into an ``input_datetime`` helper that
    the Shortcut reads. ``extra_data`` is merged into the JSON body.
    """

    enabled: bool = False
    script: str = ""
    extra_data: dict[str, Any] = Field(default_factory=dict)


class HomeAssistantConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    entity_id: str = ""
    home_zone: str = "home"
    max_age_minutes: int = Field(default=30, ge=0, le=24 * 60)
    replan_window_minutes: int = Field(default=30, ge=0, le=24 * 60)
    min_gps_accuracy_meters: int = Field(default=500, ge=0, le=1_000_000)
    zone_origins: list[ZoneOrigin] = Field(default_factory=list)
    alarm: HomeAssistantAlarmConfig = Field(default_factory=HomeAssistantAlarmConfig)
    tomorrow: HomeAssistantTomorrowConfig = Field(
        default_factory=HomeAssistantTomorrowConfig
    )


class NotifyConfig(BaseModel):
    mode: Literal["stdout", "telegram"] = "stdout"


class Config(BaseModel):
    origin: Origin
    calendars: list[CalendarSpec]
    prep: PrepConfig
    scheduling: SchedulingConfig
    paths: PathsConfig
    opencode_go: OpencodeGoConfig
    mta: MtaConfig
    location_overrides: list[LocationOverride] = []
    mode_overrides: list[ModeOverride] = []
    home_assistant: HomeAssistantConfig = HomeAssistantConfig()
    notify: NotifyConfig = NotifyConfig()
    google_maps_api_key: str = ""
    google_oauth_client_secret_json: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0
    opencode_go_token: str = ""
    home_assistant_token: str = ""


# ─────────── Domain models ───────────

class ResolvedLocation(BaseModel):
    kind: Literal["address", "station"]
    value: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    source: Literal["known_venues", "geocode", "llm", "cache", "ha_zone"]


class Event(BaseModel):
    id: str
    calendar_id: str
    calendar_name: str
    title: str
    start: datetime
    end: datetime
    location_raw: Optional[str] = None
    location_resolved: Optional[ResolvedLocation] = None
    mode_override: Optional[Literal["transit", "driving", "walking", "bicycling"]] = None


class TransitLeg(BaseModel):
    mode: Literal["WALKING", "TRANSIT", "DRIVING", "BICYCLING"]
    system: Optional[str] = None
    line: Optional[str] = None
    headsign: Optional[str] = None
    depart_at: datetime
    arrive_at: datetime
    duration_seconds: int
    summary: str
    # Structured boarding/alighting stop names.  Kept separately from ``summary``
    # so consumers (e.g. MTA alert relevance) don't have to re-parse the
    # human-readable string, which breaks on stop names containing "to"/"and".
    departure_stop: Optional[str] = None
    arrival_stop: Optional[str] = None


class Route(BaseModel):
    legs: list[TransitLeg]
    depart_at: datetime
    arrive_at: datetime
    total_duration_seconds: int
    transfers: int = 0
    fare_estimate_cents: Optional[int] = None
    raw_provider_payload: Optional[dict[str, Any]] = None
    # True when the route did not come from a live Directions response — either
    # a previously-cached route reused during an API outage, or a coarse
    # distance/speed estimate.  Surfaced in the digest so the user knows the
    # timing is best-effort rather than schedule-accurate.
    approximate: bool = False


class Plan(BaseModel):
    event: Event
    route: Optional[Route] = None
    leave_at: Optional[datetime] = None
    prep_at: Optional[datetime] = None
    error: Optional[str] = None


class Alert(BaseModel):
    id: str
    header: str
    description: str
    affected_routes: set[str] = Field(default_factory=set)
    affected_systems: set[str] = Field(default_factory=set)
    active_periods: list[tuple[datetime, Optional[datetime]]] = Field(default_factory=list)
    severity: Literal["INFO", "WARNING", "SEVERE"] = "INFO"
    url: Optional[str] = None


class PingEntry(BaseModel):
    id: str
    event_id: str
    kind: Literal["digest", "prep", "leave", "service_update"]
    fire_at: datetime
    fired: bool = False
    fired_at: Optional[datetime] = None
    message: str
    # Number of send attempts that have already failed for this ping.  Used by
    # the poll loop to bound cross-tick re-fire of actionable pings whose send
    # failed transiently (see ``Store.release_ping``).
    send_attempts: int = 0


class CurrentLocation(BaseModel):
    lat: float
    lon: float
    zone: Optional[str] = None
    captured_at: datetime
    source: str = "home_assistant"
    accuracy_m: Optional[float] = None


class ZoneInfo(BaseModel):
    """Snapshot of an HA `zone.*` entity used for origin/destination matching."""

    name: str
    lat: float
    lon: float
    radius_m: float = 0.0
    entity_id: str = ""


class AdjustRow(BaseModel):
    """One row from ``adjust_log`` — what the most recent prep shift did.

    Used by ``undo`` to restore the exact prior ``prep_at`` (rather than
    re-applying the inverse offset and re-clamping against ``now``).
    """

    key: str
    event_id: str
    applied_at: datetime
    add_prep_minutes: int
    prev_prep_at: Optional[datetime] = None
    undone: bool = False
