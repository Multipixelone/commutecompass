"""All pydantic / dataclass models."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ─────────── Configuration models ───────────

class Origin(BaseModel):
    address: str
    lat: float
    lon: float
    subway_station: str = ""
    lirr_station: str = ""


class CalendarSpec(BaseModel):
    id: str
    name: str
    enabled: bool = True


class PrepConfig(BaseModel):
    prep_minutes: int = 20
    safety_buffer_minutes: int = 5


class SchedulingConfig(BaseModel):
    morning_run_time: time = time(6, 0)
    poll_interval_seconds: int = 60
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


class HomeAssistantConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    entity_id: str = ""
    home_zone: str = "home"
    max_age_minutes: int = 30
    replan_window_minutes: int = 30


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
    source: Literal["known_venues", "geocode", "llm", "cache"]


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


class Route(BaseModel):
    legs: list[TransitLeg]
    depart_at: datetime
    arrive_at: datetime
    total_duration_seconds: int
    transfers: int = 0
    fare_estimate_cents: Optional[int] = None
    raw_provider_payload: Optional[dict[str, Any]] = None


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


class CurrentLocation(BaseModel):
    lat: float
    lon: float
    zone: Optional[str] = None
    captured_at: datetime
    source: str = "home_assistant"
