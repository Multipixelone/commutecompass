"""Config schema and loader."""

from __future__ import annotations

import os
from datetime import time as dt_time
from datetime import datetime
from pathlib import Path

import tomllib
from typing import Optional

from pydantic import BaseModel


# ─────────── Config models (mirrors models.py for loader use) ───────────

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
    morning_run_time: dt_time = dt_time(6, 0)
    poll_interval_seconds: int = 60
    quiet_hours_start: dt_time | None = None
    quiet_hours_end: dt_time | None = None


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


class ZoneOrigin(BaseModel):
    zone: str
    address: str
    lat: float
    lon: float
    subway_station: str = ""
    lirr_station: str = ""


class HomeAssistantConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    entity_id: str = ""
    home_zone: str = "home"
    max_age_minutes: int = 30
    replan_window_minutes: int = 30
    min_gps_accuracy_meters: int = 500
    zone_origins: list[ZoneOrigin] = []


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
    # Loaded from env, not TOML:
    google_maps_api_key: str = ""
    google_oauth_client_secret_json: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0
    opencode_go_token: str = ""
    home_assistant_token: str = ""


# ─────────── Exceptions ───────────

class ConfigError(Exception):
    """Raised when config is invalid or missing required values.

    Collects all missing env var names so the user sees everything
    that's broken in one error, not one per restart.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(f"missing required environment variables: {names}")


# ─────────── Public API ───────────

_REQUIRED_ENV_VARS = [
    "GOOGLE_MAPS_API_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OPENCODE_GO_TOKEN",
]

_HA_TOKEN_ENV_VAR = "HOME_ASSISTANT_TOKEN"


def load_from_env(extra_required: list[str] | None = None) -> dict[str, str]:
    """Extract required env vars; raise ConfigError listing all missing at once."""
    required = _REQUIRED_ENV_VARS + list(extra_required or [])
    missing: list[str] = []
    values: dict[str, str] = {}
    for var in required:
        val = os.environ.get(var)
        if val is None or val == "":
            missing.append(var)
        else:
            values[var] = val
    if missing:
        raise ConfigError(missing)
    return values


def load_config(toml_path: Path) -> Config:
    """Load and validate config from TOML file, merging in required env vars."""
    with open(toml_path, "rb") as fh:
        raw = tomllib.load(fh)

    # Coerce morning_run_time from string to time (TOML has "06:00", pydantic expects time)
    if "scheduling" in raw:
        sched = raw["scheduling"]
        if "morning_run_time" in sched and isinstance(sched["morning_run_time"], str):
            sched["morning_run_time"] = datetime.strptime(sched["morning_run_time"], "%H:%M").time()

    # HA token is required only when [home_assistant].enabled = true.
    ha_enabled = bool(raw.get("home_assistant", {}).get("enabled", False))
    extra = [_HA_TOKEN_ENV_VAR] if ha_enabled else []

    # Pull env vars (raises ConfigError with all missing at once)
    env = load_from_env(extra_required=extra)

    # Merge env vars into the TOML data for pydantic validation
    raw["google_maps_api_key"] = env["GOOGLE_MAPS_API_KEY"]
    raw["google_oauth_client_secret_json"] = env["GOOGLE_OAUTH_CLIENT_SECRET"]
    raw["telegram_bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    raw["telegram_chat_id"] = int(env["TELEGRAM_CHAT_ID"])
    raw["opencode_go_token"] = env["OPENCODE_GO_TOKEN"]
    if ha_enabled:
        raw["home_assistant_token"] = env[_HA_TOKEN_ENV_VAR]

    return Config.model_validate(raw)