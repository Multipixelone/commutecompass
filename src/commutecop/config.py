"""Config schema and loader."""

from __future__ import annotations

import os
from datetime import time as dt_time
from datetime import datetime
from pathlib import Path

import tomllib
from pydantic import BaseModel


# ─────────── Config models (mirrors models.py for loader use) ───────────

class Origin(BaseModel):
    address: str
    lat: float
    lon: float
    subway_station: str
    lirr_station: str


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


class Config(BaseModel):
    origin: Origin
    calendars: list[CalendarSpec]
    prep: PrepConfig
    scheduling: SchedulingConfig
    paths: PathsConfig
    opencode_go: OpencodeGoConfig
    mta: MtaConfig
    # Loaded from env, not TOML:
    google_maps_api_key: str = ""
    google_oauth_client_secret_json: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0
    opencode_go_token: str = ""


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


def load_from_env() -> dict[str, str]:
    """Extract required env vars; raise ConfigError listing all missing at once."""
    missing: list[str] = []
    values: dict[str, str] = {}
    for var in _REQUIRED_ENV_VARS:
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

    # Pull env vars (raises ConfigError with all missing at once)
    env = load_from_env()

    # Merge env vars into the TOML data for pydantic validation
    raw["google_maps_api_key"] = env["GOOGLE_MAPS_API_KEY"]
    raw["google_oauth_client_secret_json"] = env["GOOGLE_OAUTH_CLIENT_SECRET"]
    raw["telegram_bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    raw["telegram_chat_id"] = int(env["TELEGRAM_CHAT_ID"])
    raw["opencode_go_token"] = env["OPENCODE_GO_TOKEN"]

    return Config.model_validate(raw)