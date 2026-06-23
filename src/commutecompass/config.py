"""Config schema and loader."""

from __future__ import annotations

import json
import os
from datetime import time as dt_time
from datetime import datetime
from pathlib import Path

import tomllib
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ─────────── Config models (mirrors models.py for loader use) ───────────

class Origin(BaseModel):
    address: str
    # Geographic bounds — bare minimum to catch swapped lat/lon, missing
    # decimal points, etc.  Worldwide rather than NYC-specific so the same
    # validation applies if the operator generalises the tool later.
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
    morning_run_time: dt_time = dt_time(6, 0)
    poll_interval_seconds: int = Field(default=60, ge=1, le=86_400)
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


class ModeOverride(BaseModel):
    """Force a travel mode for events whose (effective) location matches.

    ``location_contains`` is matched case-insensitively as a substring against
    the event's effective location (after ``location_overrides`` are applied).
    First matching rule wins. Handy for "always bike to work".
    """

    location_contains: str
    mode: Literal["transit", "driving", "walking", "bicycling"]


class ZoneOrigin(BaseModel):
    zone: str
    address: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    subway_station: str = ""
    lirr_station: str = ""


class HomeAssistantAlarmConfig(BaseModel):
    """Additive alarm channel — see models.HomeAssistantAlarmConfig."""

    enabled: bool = False
    service: str = ""
    kinds: list[Literal["prep", "leave"]] = ["prep", "leave"]
    extra_data: dict[str, Any] = {}


class HomeAssistantTomorrowConfig(BaseModel):
    """Pull-model tomorrow alarm — see models.HomeAssistantTomorrowConfig."""

    enabled: bool = False
    script: str = ""
    extra_data: dict[str, Any] = {}


class HomeAssistantConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    entity_id: str = ""
    home_zone: str = "home"
    max_age_minutes: int = Field(default=30, ge=0, le=24 * 60)
    replan_window_minutes: int = Field(default=30, ge=0, le=24 * 60)
    # 0 disables the accuracy filter; otherwise reject readings worse than this.
    min_gps_accuracy_meters: int = Field(default=500, ge=0, le=1_000_000)
    zone_origins: list[ZoneOrigin] = []
    alarm: HomeAssistantAlarmConfig = HomeAssistantAlarmConfig()
    tomorrow: HomeAssistantTomorrowConfig = HomeAssistantTomorrowConfig()

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        """When set, the URL must start with http:// or https:// (no path traversal)."""
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"home_assistant.base_url must start with http(s)://, got {v!r}"
            )
        return v.rstrip("/")


class NotifyConfig(BaseModel):
    mode: Literal["stdout", "telegram"] = "stdout"


class WeatherConfig(BaseModel):
    """Weather-aware buffer: pad departure when rain/snow is expected.

    Uses the free Open-Meteo forecast API (no key).  When precipitation is
    likely around the commute window, extra minutes are subtracted from the
    leave time so the alarm fires earlier.
    """

    enabled: bool = False
    forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    # Extra minutes added to the buffer when rain / snow is expected.
    rain_buffer_minutes: int = Field(default=10, ge=0, le=120)
    snow_buffer_minutes: int = Field(default=20, ge=0, le=240)
    # Minimum precipitation probability (%) before the rain buffer applies.
    precip_probability_threshold: int = Field(default=50, ge=0, le=100)

    @field_validator("forecast_url")
    @classmethod
    def _validate_forecast_url(cls, v: str) -> str:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"weather.forecast_url must start with http(s)://, got {v!r}")
        return v.rstrip("/")


class MonitoringConfig(BaseModel):
    """Dead-man's-switch / heartbeat configuration.

    ``heartbeat_url`` is an optional healthchecks.io-style endpoint that the
    poll job pings on every successful run; the external service alerts when the
    pings stop (i.e. the per-minute timer died).  ``poll_staleness_minutes`` is
    the threshold past which the morning digest flags that poll has not run.
    """

    heartbeat_url: Optional[str] = None
    poll_staleness_minutes: int = Field(default=15, ge=1, le=24 * 60)

    @field_validator("heartbeat_url")
    @classmethod
    def _validate_heartbeat_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"monitoring.heartbeat_url must start with http(s)://, got {v!r}"
            )
        return v


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
    monitoring: MonitoringConfig = MonitoringConfig()
    weather: WeatherConfig = WeatherConfig()
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

_BASE_REQUIRED_ENV_VARS = [
    "GOOGLE_MAPS_API_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "OPENCODE_GO_TOKEN",
]

_TELEGRAM_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

_HA_TOKEN_ENV_VAR = "HOME_ASSISTANT_TOKEN"


def load_from_env(
    extra_required: list[str] | None = None,
    *,
    require_telegram: bool = True,
) -> dict[str, str]:
    """Extract required env vars; raise ConfigError listing all missing at once.

    Telegram credentials are required only when ``notify.mode == "telegram"``
    (the loader passes ``require_telegram`` accordingly).
    """
    required = list(_BASE_REQUIRED_ENV_VARS)
    if require_telegram:
        required.extend(_TELEGRAM_ENV_VARS)
    required.extend(extra_required or [])
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

    # Notify mode controls whether Telegram credentials are required.
    notify_mode = str(raw.get("notify", {}).get("mode", "stdout"))
    require_telegram = notify_mode == "telegram"

    # HA token is required only when [home_assistant].enabled = true.
    ha_enabled = bool(raw.get("home_assistant", {}).get("enabled", False))
    extra = [_HA_TOKEN_ENV_VAR] if ha_enabled else []

    # Pull env vars (raises ConfigError with all missing at once)
    env = load_from_env(extra_required=extra, require_telegram=require_telegram)

    # Merge env vars into the TOML data for pydantic validation
    raw["google_maps_api_key"] = env["GOOGLE_MAPS_API_KEY"]
    raw["google_oauth_client_secret_json"] = env["GOOGLE_OAUTH_CLIENT_SECRET"]
    raw["opencode_go_token"] = env["OPENCODE_GO_TOKEN"]
    if require_telegram:
        raw["telegram_bot_token"] = env["TELEGRAM_BOT_TOKEN"]
        raw["telegram_chat_id"] = int(env["TELEGRAM_CHAT_ID"])
    if ha_enabled:
        raw["home_assistant_token"] = env[_HA_TOKEN_ENV_VAR]

    return Config.model_validate(raw)


# ─────────── Display + safe-edit helpers (used by `config show` / `config set`) ───────────

# Substrings that mark a field as a secret — checked anywhere in the field
# name. Covers `*_token`, `*_secret_*`, `*_key`, `oauth_*`, etc.
_REDACT_TOKENS = ("token", "secret", "key", "oauth")
_REDACT_PLACEHOLDER = "***REDACTED***"


def _is_secret_key(leaf_key: str) -> bool:
    name = leaf_key.lower()
    return any(tok in name for tok in _REDACT_TOKENS)


def redact_for_display(data: Any) -> Any:
    """Recursively replace secret-looking values with a placeholder.

    Used by ``commutecompass config show`` so the LLM (or anyone reading
    the chat transcript) never sees raw credentials.
    """
    if isinstance(data, dict):
        return {
            k: (_REDACT_PLACEHOLDER if _is_secret_key(str(k)) and v else redact_for_display(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_for_display(item) for item in data]
    return data


def _strip_nones(data: Any) -> Any:
    """Recursively drop keys whose values are None (tomlkit cannot serialize them)."""
    if isinstance(data, dict):
        return {k: _strip_nones(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_strip_nones(item) for item in data]
    return data


# Allowlist of dotted config keys safe to edit via `config set`.  Each entry
# names a coercion function that turns the user-supplied string into the
# correct TOML type.  Any key not present here is rejected.
def _coerce_int(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"expected an integer, got {value!r}") from exc


def _coerce_hhmm(value: str) -> str:
    # Validate format; store as the canonical "HH:MM" string TOML expects.
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"expected HH:MM time (e.g. 06:00), got {value!r}") from exc
    return parsed.strftime("%H:%M")


def _coerce_notify_mode(value: str) -> str:
    if value not in ("stdout", "telegram"):
        raise ValueError(f"notify.mode must be 'stdout' or 'telegram', got {value!r}")
    return value


def _coerce_bool(value: str) -> bool:
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    v = value.strip().lower()
    if v in truthy:
        return True
    if v in falsy:
        return False
    raise ValueError(f"expected a boolean (true/false), got {value!r}")


CONFIG_SET_ALLOWLIST: dict[str, Any] = {
    "prep.prep_minutes": _coerce_int,
    "prep.safety_buffer_minutes": _coerce_int,
    "scheduling.morning_run_time": _coerce_hhmm,
    "scheduling.poll_interval_seconds": _coerce_int,
    "scheduling.quiet_hours_start": _coerce_hhmm,
    "scheduling.quiet_hours_end": _coerce_hhmm,
    "notify.mode": _coerce_notify_mode,
    # HA toggles — safe to flip from chat ("turn off the loud alarm").
    "home_assistant.alarm.enabled": _coerce_bool,
    "home_assistant.tomorrow.enabled": _coerce_bool,
    "home_assistant.replan_window_minutes": _coerce_int,
    "home_assistant.max_age_minutes": _coerce_int,
}


# Keys that the app itself never reads at runtime — an external scheduler
# (systemd timer, cron) drives when `morning`/`poll` run, so editing these in
# TOML records intent but does NOT change the schedule.  The CLI warns when one
# is set so a chat user isn't misled into thinking it took effect.
EXTERNALLY_SCHEDULED_KEYS: frozenset[str] = frozenset(
    {"scheduling.morning_run_time", "scheduling.poll_interval_seconds"}
)


class ConfigSetError(Exception):
    """Raised by ``update_config_field`` for an invalid key or value."""


def _split_dotted(dotted_key: str) -> tuple[list[str], str]:
    """Split a dotted key into (intermediate_tables, leaf_key).

    For ``prep.prep_minutes`` returns ``(["prep"], "prep_minutes")``.  For
    ``home_assistant.alarm.enabled`` returns
    ``(["home_assistant", "alarm"], "enabled")``.
    """
    parts = dotted_key.split(".")
    if len(parts) < 2:
        raise ConfigSetError(
            f"{dotted_key!r} must be of the form 'section.key' (or 'section.subsection.key')"
        )
    return parts[:-1], parts[-1]


def _walk_or_create(doc: Any, path_parts: list[str]) -> Any:
    """Walk into ``doc`` along ``path_parts``, creating empty tables as needed."""
    import tomlkit

    node = doc
    for p in path_parts:
        sub = node.get(p) if hasattr(node, "get") else None
        if sub is None:
            sub = tomlkit.table()
            node[p] = sub
        node = sub
    return node


def _walk_or_none(doc: Any, path_parts: list[str]) -> Any:
    """Walk into ``doc`` along ``path_parts``; return None if any segment is missing."""
    node = doc
    for p in path_parts:
        if not hasattr(node, "get"):
            return None
        sub = node.get(p)
        if sub is None:
            return None
        node = sub
    return node


def update_config_field(toml_path: Path, dotted_key: str, value: str) -> Any:
    """Surgically update a single allowlisted field in ``toml_path``.

    Uses ``tomlkit`` so comments and formatting are preserved.  Returns the
    coerced value that was written, for the CLI to echo.  Raises
    ``ConfigSetError`` if the key is not on the allowlist or the value cannot
    be coerced.
    """
    if dotted_key not in CONFIG_SET_ALLOWLIST:
        allowed = ", ".join(sorted(CONFIG_SET_ALLOWLIST))
        raise ConfigSetError(
            f"{dotted_key!r} is not editable from `config set`. Allowed: {allowed}"
        )

    coerce = CONFIG_SET_ALLOWLIST[dotted_key]
    try:
        coerced = coerce(value)
    except ValueError as exc:
        raise ConfigSetError(str(exc)) from exc

    # Import locally so test environments without tomlkit don't pay the cost.
    import tomlkit

    with open(toml_path, encoding="utf-8") as fh:
        doc = tomlkit.parse(fh.read())

    path_parts, leaf = _split_dotted(dotted_key)
    node = _walk_or_create(doc, path_parts)
    node[leaf] = coerced

    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(tomlkit.dumps(doc))

    return coerced


def delete_config_field(toml_path: Path, dotted_key: str) -> bool:
    """Delete an allowlisted field from ``toml_path`` so the Pydantic default applies.

    Returns True if a key was removed, False if it was already absent.  Raises
    ``ConfigSetError`` for keys outside ``CONFIG_SET_ALLOWLIST`` — the same
    refusal surface as ``update_config_field`` keeps secrets/paths immutable
    via the skill.
    """
    if dotted_key not in CONFIG_SET_ALLOWLIST:
        allowed = ", ".join(sorted(CONFIG_SET_ALLOWLIST))
        raise ConfigSetError(
            f"{dotted_key!r} is not unsettable from `config unset`. Allowed: {allowed}"
        )

    import tomlkit

    with open(toml_path, encoding="utf-8") as fh:
        doc = tomlkit.parse(fh.read())

    path_parts, leaf = _split_dotted(dotted_key)
    node = _walk_or_none(doc, path_parts)
    if node is None or leaf not in node:
        return False
    del node[leaf]

    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(tomlkit.dumps(doc))
    return True


def list_overridden_allowlist_keys(toml_path: Path) -> list[str]:
    """Return every allowlisted dotted key currently present in ``toml_path``.

    Used by ``config reset`` to preview what would be removed before the user
    confirms with ``--yes``.
    """
    import tomlkit

    with open(toml_path, encoding="utf-8") as fh:
        doc = tomlkit.parse(fh.read())

    present: list[str] = []
    for key in CONFIG_SET_ALLOWLIST:
        path_parts, leaf = _split_dotted(key)
        node = _walk_or_none(doc, path_parts)
        if node is not None and hasattr(node, "get") and leaf in node:
            present.append(key)
    return present


def render_config_toml(cfg: Config) -> str:
    """Render a redacted Config back to TOML for `config show`.

    None-valued fields are dropped (TOML has no nullable scalar). Use the JSON
    form if you need to see those.
    """
    import tomlkit

    data = _strip_nones(redact_for_display(cfg.model_dump(mode="json")))
    return tomlkit.dumps(data)


def render_config_json(cfg: Config) -> str:
    """Render a redacted Config as pretty JSON for `config show --json`."""
    data = redact_for_display(cfg.model_dump(mode="json"))
    return json.dumps(data, indent=2, sort_keys=True)