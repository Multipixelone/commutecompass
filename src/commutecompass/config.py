"""Config schema and loader."""

from __future__ import annotations

import json
import os
from datetime import time as dt_time
from datetime import datetime
from pathlib import Path

import tomllib
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Static GTFS identity-bridge index written by `gtfs-refresh` (kept separate
    # from the state DB).  Defaults to `gtfs.db` next to `db_path`.
    gtfs_db_path: str = ""

    @model_validator(mode="after")
    def _default_gtfs_db_path(self) -> "PathsConfig":
        if not self.gtfs_db_path:
            self.gtfs_db_path = str(Path(self.db_path).with_name("gtfs.db"))
        self.ensure_distinct_db_paths()
        return self

    def ensure_distinct_db_paths(self) -> None:
        """Reject aliases of the state DB before a GTFS refresh can replace it.

        Resolve even missing targets to catch relative paths, ``..`` and
        symlinked parents; samefile also catches existing hardlinks.  Reusable
        at command entry because model fields and filesystem aliases can change
        after initial validation.  This is not a filesystem race protection.
        """
        state_db = Path(self.db_path)
        gtfs_db = Path(self.gtfs_db_path)
        try:
            collision = state_db.resolve() == gtfs_db.resolve()
            if not collision:
                try:
                    collision = state_db.samefile(gtfs_db)
                except FileNotFoundError:
                    pass  # New databases need not exist yet.
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"cannot verify paths.db_path and paths.gtfs_db_path: {exc}") from exc
        if collision:
            raise ValueError(
                "paths.gtfs_db_path must be separate from paths.db_path "
                "(both refer to the same database)"
            )


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


# Canonical MTA GTFS-RT *trip-update* feeds (distinct from the alert feeds in
# mta.py).  Subway is split into line-group feeds; verify against
# https://api.mta.info.  Bus trip-updates come from OneBusAway.
_MTA_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds"
_DEFAULT_SUBWAY_TRIPUPDATE_URLS = [
    f"{_MTA_BASE}/nyct%2Fgtfs",  # 1 2 3 4 5 6 7 S
    f"{_MTA_BASE}/nyct%2Fgtfs-ace",
    f"{_MTA_BASE}/nyct%2Fgtfs-bdfm",
    f"{_MTA_BASE}/nyct%2Fgtfs-g",
    f"{_MTA_BASE}/nyct%2Fgtfs-jz",
    f"{_MTA_BASE}/nyct%2Fgtfs-nqrw",
    f"{_MTA_BASE}/nyct%2Fgtfs-l",
    f"{_MTA_BASE}/nyct%2Fgtfs-si",
]
_DEFAULT_LIRR_TRIPUPDATE_URL = f"{_MTA_BASE}/lirr%2Fgtfs-lirr"
_DEFAULT_BUS_TRIPUPDATE_URL = "https://gtfsrt.prod.obanyc.com/tripUpdates"


class RealtimeConfig(BaseModel):
    """Real-time departure buffer: pad leave time when the boarding line runs late.

    Reads the MTA GTFS-RT *trip-update* feeds, fuzzy-matches the boarding stop to
    a GTFS ``stop_id`` (via the bundled ``data/stops_*.csv``), and folds an
    observed delay into the buffer so the alarm fires earlier.  Conservative by
    design: a delay can only move the leave time *earlier*, never later, and is
    capped — see :mod:`commutecompass.realtime`.  Disabled by default.
    """

    enabled: bool = False
    subway_tripupdate_urls: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_SUBWAY_TRIPUPDATE_URLS)
    )
    lirr_tripupdate_url: str = _DEFAULT_LIRR_TRIPUPDATE_URL
    bus_tripupdate_url: str = _DEFAULT_BUS_TRIPUPDATE_URL
    # Most an observed delay can add to the buffer (guards against bad feed data).
    max_buffer_minutes: int = Field(default=15, ge=0, le=120)
    # Ignore delays smaller than this (feed jitter / rounding noise).
    min_delay_minutes: int = Field(default=2, ge=0, le=60)
    # How far from the scheduled departure to look for the matching trip.
    match_window_minutes: int = Field(default=30, ge=1, le=180)
    # rapidfuzz score cutoff (0-100) for boarding-stop name matching.
    fuzzy_threshold: int = Field(default=80, ge=0, le=100)

    @field_validator("subway_tripupdate_urls")
    @classmethod
    def _validate_subway_urls(cls, v: list[str]) -> list[str]:
        for url in v:
            if url and not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(
                    f"realtime.subway_tripupdate_urls entries must start with http(s)://, got {url!r}"
                )
        return v

    @field_validator("lirr_tripupdate_url", "bus_tripupdate_url")
    @classmethod
    def _validate_tripupdate_url(cls, v: str) -> str:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"realtime trip-update URL must start with http(s)://, got {v!r}")
        return v


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


class AlarmConfig(BaseModel):
    """Wake & nap alarm — see docs/alarm-design.md.

    commutecompass publishes the computed wake time to HA; HA owns nap logic
    and the final "next alarm" selection; ``alarm daemon`` rings link's
    speakers.  Entity/script ids are HA ids (``domain.object_id``).
    """

    enabled: bool = False
    # HH:MM NYC wall-clock time on the target logical day.
    fallback_time: str = "12:00"
    wake_lead_minutes: int = Field(default=0, ge=0, le=24 * 60)
    publish_script: str = "script.commute_publish_alarm"
    nap_script: str = "script.commute_nap"
    skip_script: str = "script.commute_alarm_skip"
    next_alarm_entity: str = "sensor.commute_next_alarm"
    enabled_entity: str = "input_boolean.commute_alarm_enabled"
    stop_button: str = "input_button.commute_alarm_stop"
    snooze_button: str = "input_button.commute_alarm_snooze"
    state_entity: str = "input_select.commute_alarm_state"
    heartbeat_entity: str = "input_datetime.commute_alarm_heartbeat"
    # Case-insensitive substring of the PipeWire sink description/name.
    sink: str = "analog"
    # Path to a sound file; empty = synthesized tone.
    sound: str = ""
    ramp_start_percent: int = Field(default=30, ge=0, le=100)
    ramp_seconds: int = Field(default=120, ge=0, le=3600)
    max_ring_minutes: int = Field(default=15, ge=1, le=240)
    snooze_minutes: int = Field(default=9, ge=1, le=240)
    max_snoozes: int = Field(default=2, ge=0, le=20)
    poll_seconds: int = Field(default=30, ge=1, le=3600)
    # Empty = <db dir>/alarm-cache.json
    cache_path: str = ""

    @field_validator("fallback_time")
    @classmethod
    def _validate_fallback_time(cls, v: str) -> str:
        try:
            parsed = datetime.strptime(v, "%H:%M").time()
        except ValueError as exc:
            raise ValueError(f"alarm.fallback_time must be HH:MM, got {v!r}") from exc
        return parsed.strftime("%H:%M")

    def fallback_clock(self) -> dt_time:
        """Return ``fallback_time`` as a naive ``time`` (NYC wall clock)."""
        return datetime.strptime(self.fallback_time, "%H:%M").time()


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
    realtime: RealtimeConfig = RealtimeConfig()
    alarm: AlarmConfig = AlarmConfig()
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
    # Real-time departure buffer — safe to toggle / tune from chat.
    "realtime.enabled": _coerce_bool,
    "realtime.max_buffer_minutes": _coerce_int,
    # Wake alarm tuning — safe to tweak from chat ("wake me 15 min earlier").
    "alarm.fallback_time": _coerce_hhmm,
    "alarm.wake_lead_minutes": _coerce_int,
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