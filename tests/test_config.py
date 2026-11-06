"""Tests for config.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from commutecompass.config import (
    CONFIG_SET_ALLOWLIST,
    Config,
    ConfigError,
    ConfigSetError,
    format_validation_error,
    load_config,
    load_from_env,
    redact_for_display,
    render_config_json,
    render_config_toml,
    update_config_field,
)

# ─────────── Fixtures ───────────

@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
    return _write_minimal_toml(tmp_path)


def _write_minimal_toml(tmp_path: Path) -> Path:
    """A minimal valid TOML file covering all sections."""
    content = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20
safety_buffer_minutes = 5

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60
quiet_hours_start = "22:00"
quiet_hours_end = "07:00"

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"
model = "deepseek-v4-flash"

[mta]
subway_alerts_url = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.pb"
lirr_alerts_url   = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Flirr-alerts"
bus_alerts_url    = "https://gtfsrt.prod.obanyc.com/alerts"

[[calendars]]
id = "theatre-calendar@example.com"
name = "Theatre"

[[calendars]]
id = "school-calendar@example.com"
name = "School"
"""
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


@pytest.fixture
def required_env() -> dict[str, str]:
    return {
        "GOOGLE_MAPS_API_KEY": "AIza_SyStAr_Bq7DemoKey12345",
        "GOOGLE_OAUTH_CLIENT_SECRET": '{"installed":{"client_id":"demo","client_secret":"demo"}}',
        "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrSTUvwxyz",
        "TELEGRAM_CHAT_ID": "-987654321",
        "OPENCODE_GO_TOKEN": "sk-opencode-go-demo-token",
    }


def _apply_env(env: dict[str, str]) -> None:
    for key, val in env.items():
        os.environ[key] = val


def _clear_env(keys: list[str]) -> None:
    for key in keys:
        os.environ.pop(key, None)


def test_calendar_advance_config_round_trip(
    minimal_toml: Path, required_env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from commutecompass.config import delete_config_field, list_overridden_allowlist_keys

    for key, value in required_env.items():
        monkeypatch.setenv(key, value)
    key = "alarm.window.calendar_advance_minutes"
    assert load_config(minimal_toml).alarm.window.calendar_advance_minutes == 0
    assert update_config_field(minimal_toml, key, "15") == 15
    cfg = load_config(minimal_toml)
    assert cfg.alarm.window.calendar_advance_minutes == 15
    assert "calendar_advance_minutes = 15" in render_config_toml(cfg)
    assert key in list_overridden_allowlist_keys(minimal_toml)
    for invalid in ("-1", "1441", "fifteen"):
        before = minimal_toml.read_bytes()
        with pytest.raises(ConfigSetError):
            update_config_field(minimal_toml, key, invalid)
        assert minimal_toml.read_bytes() == before
    assert delete_config_field(minimal_toml, key)
    assert load_config(minimal_toml).alarm.window.calendar_advance_minutes == 0
    assert key not in list_overridden_allowlist_keys(minimal_toml)


def test_validation_formatter_only_renders_schema_fields() -> None:
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError

    sentinel = "SYNTHETIC_SECRET_DO_NOT_DISPLAY"
    exc = ValidationError.from_exception_data("Config", [{
        "type": PydanticCustomError(sentinel, sentinel, {"secret": sentinel}),
        "loc": ("monitoring", "heartbeat_url", sentinel),
        "input": {"secret": sentinel},
    }])
    assert format_validation_error(exc) == (
        "Invalid config: monitoring.heartbeat_url: invalid value"
    )


@pytest.mark.parametrize(("content", "diagnostic"), [
    ('scheduling = "SYNTHETIC_SECRET"', "scheduling: expected section/table"),
    ('notify = 42', "notify: expected section/table"),
    ('home_assistant = []', "home_assistant: expected section/table"),
    ('[scheduling]\nmorning_run_time = "99:99SYNTHETIC_SECRET"',
     "scheduling.morning_run_time: expected HH:MM time"),
    ('bad = "SYNTHETIC_SECRET', "invalid TOML config file"),
])
def test_loader_normalizes_expected_errors(
    tmp_path: Path, content: str, diagnostic: str,
) -> None:
    path = tmp_path / "synthetic.toml"
    path.write_text(content)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert str(exc.value) == diagnostic
    assert exc.value.missing == []


def test_loader_normalizes_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "missing.toml")


# ─────────── Tests ───────────

class TestLoadFromEnv:
    def test_all_present_no_error(self, required_env: dict[str, str]) -> None:
        _apply_env(required_env)
        result = load_from_env()
        assert result["GOOGLE_MAPS_API_KEY"] == required_env["GOOGLE_MAPS_API_KEY"]
        assert result["TELEGRAM_CHAT_ID"] == required_env["TELEGRAM_CHAT_ID"]

    def test_single_missing_raises_ConfigError_with_all_missing(self, required_env: dict[str, str]) -> None:
        """When one var is missing, the error lists every missing var."""
        _apply_env(required_env)
        _clear_env(["GOOGLE_MAPS_API_KEY"])
        err = pytest.raises(ConfigError, load_from_env)
        assert "GOOGLE_MAPS_API_KEY" in str(err.value)
        # Others still present are not in missing list
        assert err.value.missing == ["GOOGLE_MAPS_API_KEY"]

    def test_three_missing_reports_all_three(self, required_env: dict[str, str]) -> None:
        """When three vars are missing, all three appear in the error."""
        _apply_env(required_env)
        _clear_env(["GOOGLE_MAPS_API_KEY", "TELEGRAM_BOT_TOKEN", "OPENCODE_GO_TOKEN"])
        err = pytest.raises(ConfigError, load_from_env)
        assert set(err.value.missing) == {"GOOGLE_MAPS_API_KEY", "TELEGRAM_BOT_TOKEN", "OPENCODE_GO_TOKEN"}
        assert "GOOGLE_MAPS_API_KEY" in str(err.value)
        assert "TELEGRAM_BOT_TOKEN" in str(err.value)
        assert "OPENCODE_GO_TOKEN" in str(err.value)

    def test_empty_string_treated_as_missing(self, required_env: dict[str, str]) -> None:
        """An env var set to '' counts as missing."""
        _apply_env(required_env)
        os.environ["GOOGLE_MAPS_API_KEY"] = ""
        err = pytest.raises(ConfigError, load_from_env)
        assert "GOOGLE_MAPS_API_KEY" in err.value.missing


class TestLoadConfig:
    def test_happy_path(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        """Full TOML + full env produces a valid Config (telegram mode forces creds to load)."""
        _apply_env(required_env)
        # Default notify.mode is "stdout"; switch to "telegram" so telegram_* fields populate.
        with open(minimal_toml, "a") as fh:
            fh.write('\n[notify]\nmode = "telegram"\n')
        cfg = load_config(minimal_toml)

        assert isinstance(cfg, Config)
        assert cfg.origin.address == "123 Example Ave, Brooklyn, NY 11201"
        assert cfg.origin.lat == 40.6950
        assert cfg.origin.subway_station == "Jay St-MetroTech"

        assert cfg.prep.prep_minutes == 20
        assert cfg.scheduling.morning_run_time.hour == 6

        assert cfg.google_maps_api_key == required_env["GOOGLE_MAPS_API_KEY"]
        assert cfg.google_oauth_client_secret_json == required_env["GOOGLE_OAUTH_CLIENT_SECRET"]
        assert cfg.telegram_bot_token == required_env["TELEGRAM_BOT_TOKEN"]
        assert cfg.telegram_chat_id == -987654321  # parsed from string
        assert cfg.opencode_go_token == required_env["OPENCODE_GO_TOKEN"]

        assert len(cfg.calendars) == 2
        assert cfg.calendars[0].name == "Theatre"
        assert cfg.calendars[1].enabled is True  # default

    def test_default_notify_mode_is_stdout_and_skips_telegram_env(
        self, minimal_toml: Path, required_env: dict[str, str]
    ) -> None:
        """With default notify.mode = "stdout", telegram env vars are not required."""
        _apply_env(required_env)
        _clear_env(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        cfg = load_config(minimal_toml)
        assert cfg.notify.mode == "stdout"
        assert cfg.telegram_bot_token == ""
        assert cfg.telegram_chat_id == 0

    def test_missing_env_raises_before_parsing_toml(self, minimal_toml: Path) -> None:
        """With no env vars set in default (stdout) mode, only the 3 always-required vars are reported."""
        _clear_env([
            "GOOGLE_MAPS_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "OPENCODE_GO_TOKEN",
        ])
        err = pytest.raises(ConfigError, load_config, minimal_toml)
        # stdout mode is default; only non-telegram vars are required
        assert set(err.value.missing) == {
            "GOOGLE_MAPS_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "OPENCODE_GO_TOKEN",
        }

    def test_missing_telegram_env_in_telegram_mode_raises(
        self, minimal_toml: Path, required_env: dict[str, str]
    ) -> None:
        """When notify.mode = "telegram", missing TELEGRAM_CHAT_ID is reported."""
        _apply_env(required_env)
        _clear_env(["TELEGRAM_CHAT_ID"])
        with open(minimal_toml, "a") as fh:
            fh.write('\n[notify]\nmode = "telegram"\n')
        err = pytest.raises(ConfigError, load_config, minimal_toml)
        assert err.value.missing == ["TELEGRAM_CHAT_ID"]

    def test_toml_time_parsed_from_string(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        """scheduling.morning_run_time as "HH:MM" string becomes time(6, 0)."""
        _apply_env(required_env)
        cfg = load_config(minimal_toml)
        from datetime import time
        assert cfg.scheduling.morning_run_time == time(6, 0)

    def test_default_values_applied(self, tmp_path: Path, required_env: dict[str, str]) -> None:
        """Optional fields that are absent from TOML get their pydantic defaults."""
        _apply_env(required_env)
        minimal = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway.pb"
lirr_alerts_url   = "https://example.com/lirr.pb"
bus_alerts_url    = "https://example.com/bus.pb"

[[calendars]]
id = "00000000-0000-0000-0000-000000000001"
name = "Test Calendar"
"""
        p = tmp_path / "config.toml"
        p.write_text(minimal)
        cfg = load_config(p)
        # Defaults from pydantic
        assert cfg.opencode_go.model == "deepseek-v4-flash"
        assert cfg.prep.prep_minutes == 20
        assert cfg.prep.safety_buffer_minutes == 5


class TestLocationOverrides:
    def test_backward_compat_no_overrides_section(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        """Config without location_overrides section yields empty list."""
        _apply_env(required_env)
        cfg = load_config(minimal_toml)
        assert cfg.location_overrides == []

    def test_override_with_title_contains(self, tmp_path: Path, required_env: dict[str, str]) -> None:
        """Override with title_contains populates correctly."""
        _apply_env(required_env)
        toml = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway"
lirr_alerts_url   = "https://example.com/lirr"
bus_alerts_url    = "https://example.com/bus"

[[calendars]]
id = "job-cal"
name = "Job"

[[location_overrides]]
calendar_id = "job-cal"
title_contains = "Office Hours"
location = "200 Example St, New York, NY 10001"
"""
        p = tmp_path / "config.toml"
        p.write_text(toml)
        cfg = load_config(p)

        assert len(cfg.location_overrides) == 1
        ov = cfg.location_overrides[0]
        assert ov.calendar_id == "job-cal"
        assert ov.title_contains == "Office Hours"
        assert ov.location == "200 Example St, New York, NY 10001"

    def test_override_without_title_contains(self, tmp_path: Path, required_env: dict[str, str]) -> None:
        """Override with only calendar_id applies to all events in that calendar."""
        _apply_env(required_env)
        toml = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway"
lirr_alerts_url   = "https://example.com/lirr"
bus_alerts_url    = "https://example.com/bus"

[[calendars]]
id = "job-cal"
name = "Job"

[[location_overrides]]
calendar_id = "job-cal"
location = "200 Example St, New York, NY 10001"
"""
        p = tmp_path / "config.toml"
        p.write_text(toml)
        cfg = load_config(p)

        assert len(cfg.location_overrides) == 1
        ov = cfg.location_overrides[0]
        assert ov.calendar_id == "job-cal"
        assert ov.title_contains is None
        assert ov.location == "200 Example St, New York, NY 10001"

    def test_multiple_overrides(self, tmp_path: Path, required_env: dict[str, str]) -> None:
        """Multiple overrides with different calendars are all parsed."""
        _apply_env(required_env)
        toml = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway"
lirr_alerts_url   = "https://example.com/lirr"
bus_alerts_url    = "https://example.com/bus"

[[calendars]]
id = "cal-a"
name = "Calendar A"

[[calendars]]
id = "cal-b"
name = "Calendar B"

[[location_overrides]]
calendar_id = "cal-a"
title_contains = "Morning"
location = "200 Example St, New York, NY 10001"

[[location_overrides]]
calendar_id = "cal-b"
location = "123 Example Ave, Brooklyn, NY 11201"
"""
        p = tmp_path / "config.toml"
        p.write_text(toml)
        cfg = load_config(p)

        assert len(cfg.location_overrides) == 2
        assert cfg.location_overrides[0].calendar_id == "cal-a"
        assert cfg.location_overrides[1].calendar_id == "cal-b"


class TestModeOverrides:
    def test_backward_compat_no_mode_overrides_section(
        self, minimal_toml: Path, required_env: dict[str, str]
    ) -> None:
        """Config without a mode_overrides section yields an empty list."""
        _apply_env(required_env)
        cfg = load_config(minimal_toml)
        assert cfg.mode_overrides == []

    def test_mode_override_parses(self, tmp_path: Path, required_env: dict[str, str]) -> None:
        """A [[mode_overrides]] block parses into a ModeOverride."""
        _apply_env(required_env)
        toml = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway"
lirr_alerts_url   = "https://example.com/lirr"
bus_alerts_url    = "https://example.com/bus"

[[calendars]]
id = "job-cal"
name = "Job"

[[mode_overrides]]
location_contains = "200 Example St"
mode = "bicycling"
"""
        p = tmp_path / "config.toml"
        p.write_text(toml)
        cfg = load_config(p)

        assert len(cfg.mode_overrides) == 1
        ov = cfg.mode_overrides[0]
        assert ov.location_contains == "200 Example St"
        assert ov.mode == "bicycling"


class TestHomeAssistant:
    """HOME_ASSISTANT_TOKEN is required only when [home_assistant].enabled = true."""

    _HA_ON = """
[origin]
address = "123 Example Ave, Brooklyn, NY 11201"
lat = 40.6950
lon = -73.9890
subway_station = "Jay St-MetroTech"
lirr_station = "Atlantic Terminal"

[prep]
prep_minutes = 20

[scheduling]
morning_run_time = "06:00"
poll_interval_seconds = 60

[paths]
venues_file = "/etc/commutecompass/known_venues.yaml"
db_path = "/var/lib/commutecompass/state.db"
oauth_token_path = "/var/lib/commutecompass/google_token.json"

[opencode_go]
endpoint = "https://opencode-go.example/v1/chat/completions"

[mta]
subway_alerts_url = "https://example.com/subway"
lirr_alerts_url   = "https://example.com/lirr"
bus_alerts_url    = "https://example.com/bus"

[[calendars]]
id = "x"
name = "X"

[home_assistant]
enabled = true
base_url = "http://ha"
entity_id = "device_tracker.iphone"
"""

    def test_ha_disabled_does_not_require_token(
        self, minimal_toml: Path, required_env: dict[str, str]
    ) -> None:
        _apply_env(required_env)
        _clear_env(["HOME_ASSISTANT_TOKEN"])
        cfg = load_config(minimal_toml)
        assert cfg.home_assistant.enabled is False
        assert cfg.home_assistant_token == ""

    def test_ha_enabled_requires_token(
        self, tmp_path: Path, required_env: dict[str, str]
    ) -> None:
        _apply_env(required_env)
        _clear_env(["HOME_ASSISTANT_TOKEN"])
        p = tmp_path / "config.toml"
        p.write_text(self._HA_ON)
        err = pytest.raises(ConfigError, load_config, p)
        assert "HOME_ASSISTANT_TOKEN" in err.value.missing

    def test_ha_enabled_token_present_loads(
        self, tmp_path: Path, required_env: dict[str, str]
    ) -> None:
        _apply_env(required_env)
        os.environ["HOME_ASSISTANT_TOKEN"] = "ha-tok"
        try:
            p = tmp_path / "config.toml"
            p.write_text(self._HA_ON)
            cfg = load_config(p)
            assert cfg.home_assistant.enabled is True
            assert cfg.home_assistant.entity_id == "device_tracker.iphone"
            assert cfg.home_assistant_token == "ha-tok"
            # Phase 1 defaults
            assert cfg.home_assistant.min_gps_accuracy_meters == 500
            assert cfg.home_assistant.zone_origins == []
        finally:
            os.environ.pop("HOME_ASSISTANT_TOKEN", None)

    def test_ha_zone_origins_parse(
        self, tmp_path: Path, required_env: dict[str, str]
    ) -> None:
        _apply_env(required_env)
        os.environ["HOME_ASSISTANT_TOKEN"] = "ha-tok"
        toml_with_zones = self._HA_ON + """
min_gps_accuracy_meters = 200

[[home_assistant.zone_origins]]
zone = "work"
address = "200 W Street, NY"
lat = 40.7346
lon = -74.0055
subway_station = "34 St-Penn Station"

[[home_assistant.zone_origins]]
zone = "cap21"
address = "18 Bridge St, NY"
lat = 40.7062
lon = -74.0124
"""
        try:
            p = tmp_path / "config.toml"
            p.write_text(toml_with_zones)
            cfg = load_config(p)
            assert cfg.home_assistant.min_gps_accuracy_meters == 200
            assert len(cfg.home_assistant.zone_origins) == 2
            zo = cfg.home_assistant.zone_origins[0]
            assert zo.zone == "work"
            assert zo.subway_station == "34 St-Penn Station"
            assert cfg.home_assistant.zone_origins[1].zone == "cap21"
        finally:
            os.environ.pop("HOME_ASSISTANT_TOKEN", None)

# ─────────── Redaction ────────────────────────────────────────────────────────


class TestRedactForDisplay:
    def test_redacts_token_suffix(self) -> None:
        result = redact_for_display({"telegram_bot_token": "123:abc"})
        assert result == {"telegram_bot_token": "***REDACTED***"}

    def test_redacts_secret_suffix(self) -> None:
        result = redact_for_display(
            {"google_oauth_client_secret_json": '{"client":"x"}'}
        )
        assert result == {"google_oauth_client_secret_json": "***REDACTED***"}

    def test_redacts_key_suffix(self) -> None:
        result = redact_for_display({"google_maps_api_key": "AIza..."})
        assert result == {"google_maps_api_key": "***REDACTED***"}

    def test_preserves_non_secret_fields(self) -> None:
        data = {"prep": {"prep_minutes": 20}, "origin": {"address": "x"}}
        assert redact_for_display(data) == data

    def test_recursive_into_nested(self) -> None:
        data = {"outer": {"home_assistant_token": "secret", "enabled": True}}
        result = redact_for_display(data)
        assert result["outer"]["home_assistant_token"] == "***REDACTED***"
        assert result["outer"]["enabled"] is True

    def test_empty_secret_stays_empty(self) -> None:
        """Don't replace empty/zero values — only non-empty secrets get redacted."""
        result = redact_for_display({"telegram_bot_token": ""})
        assert result == {"telegram_bot_token": ""}


# ─────────── render_config_toml / json ───────────────────────────────────────


class TestRenderConfig:
    def _cfg(self, required_env: dict[str, str], minimal_toml: Path) -> Config:
        _apply_env(required_env)
        with open(minimal_toml, "a") as fh:
            fh.write('\n[notify]\nmode = "telegram"\n')
        return load_config(minimal_toml)

    def test_render_toml_redacts(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        cfg = self._cfg(required_env, minimal_toml)
        out = render_config_toml(cfg)
        assert "***REDACTED***" in out
        assert required_env["TELEGRAM_BOT_TOKEN"] not in out
        assert required_env["GOOGLE_MAPS_API_KEY"] not in out

    def test_render_json_redacts(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        cfg = self._cfg(required_env, minimal_toml)
        out = render_config_json(cfg)
        assert "***REDACTED***" in out
        assert required_env["TELEGRAM_BOT_TOKEN"] not in out


# ─────────── update_config_field ─────────────────────────────────────────────


@pytest.mark.parametrize(("key", "value"), [
    ("scheduling.poll_interval_seconds", "0"),
    ("prep.prep_minutes", "-1"),
    ("prep.prep_minutes", "1441"),
])
def test_invalid_update_leaves_document_unchanged(minimal_toml: Path, key: str, value: str) -> None:
    before = minimal_toml.read_bytes()
    with pytest.raises(ConfigSetError, match="Invalid config"):
        update_config_field(minimal_toml, key, value)
    assert minimal_toml.read_bytes() == before


def test_update_validates_entire_document(minimal_toml: Path) -> None:
    minimal_toml.write_text(minimal_toml.read_text().replace('lat = 40.6950', 'lat = 999'))
    before = minimal_toml.read_bytes()
    with pytest.raises(ConfigSetError, match="Invalid config"):
        update_config_field(minimal_toml, "prep.prep_minutes", "30")
    assert minimal_toml.read_bytes() == before


@pytest.mark.parametrize("field", [
    "google_maps_api_key", "google_oauth_client_secret_json", "telegram_bot_token",
    "opencode_go_token", "home_assistant_token",
])
def test_all_config_secrets_redacted(minimal_toml: Path, field: str) -> None:
    import tomllib

    assert field in Config.model_fields
    config = Config.model_validate(tomllib.loads(minimal_toml.read_text())).model_dump()
    config[field] = "sensitive-value"
    assert redact_for_display(config)[field] == "***REDACTED***"


@pytest.mark.parametrize("field", ["password", "credential", "bearer"])
def test_additional_secret_keywords_redacted(field: str) -> None:
    assert redact_for_display({"nested": [{field: "sensitive-value"}]}) == {
        "nested": [{field: "***REDACTED***"}]
    }


def test_explicit_secret_paths_do_not_depend_on_heuristics(monkeypatch: pytest.MonkeyPatch) -> None:
    import commutecompass.config as config_module

    monkeypatch.setattr(config_module, "_REDACT_TOKENS", ())
    secrets = {field: "sensitive-value" for field in (
        "google_maps_api_key", "google_oauth_client_secret_json", "telegram_bot_token",
        "opencode_go_token", "home_assistant_token",
    )}
    assert set(redact_for_display(secrets).values()) == {"***REDACTED***"}


@pytest.mark.parametrize(("key", "value"), [
    ("scheduling.morning_run_time", "07:15"),
    ("scheduling.poll_interval_seconds", "30"),
])
def test_externally_scheduled_updates_warn(minimal_toml: Path, key: str, value: str) -> None:
    from click.testing import CliRunner

    from commutecompass.cli import cli

    result = CliRunner().invoke(cli, ["--config", str(minimal_toml), "config", "set", key, value])
    assert result.exit_code == 0, result.output
    assert "not read at runtime" in result.output
    assert "systemd timer" in result.output


class TestUpdateConfigField:
    def _write_toml(self, tmp_path: Path) -> Path:
        p = _write_minimal_toml(tmp_path)
        original = p.read_text()
        p.write_text("# Top-level comment that must survive\n" + original.replace(
            "prep_minutes = 20", "prep_minutes = 20  # inline comment"
        ))
        return p

    def test_allowlist_unknown_key_rejected(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        with pytest.raises(ConfigSetError) as exc_info:
            update_config_field(p, "telegram_bot_token", "abc")
        # Error names the offending key and lists allowed ones
        assert "telegram_bot_token" in str(exc_info.value)
        assert "prep.prep_minutes" in str(exc_info.value)

    def test_int_value_coerced_and_written(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        result = update_config_field(p, "prep.prep_minutes", "30")
        assert result == 30
        body = p.read_text()
        assert "prep_minutes = 30" in body

    def test_invalid_int_raises(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        with pytest.raises(ConfigSetError, match="integer"):
            update_config_field(p, "prep.prep_minutes", "thirty")

    def test_hhmm_value_validated_and_written(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        result = update_config_field(p, "scheduling.morning_run_time", "07:15")
        assert result == "07:15"
        body = p.read_text()
        assert 'morning_run_time = "07:15"' in body

    def test_invalid_hhmm_raises(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        with pytest.raises(ConfigSetError, match="HH:MM"):
            update_config_field(p, "scheduling.morning_run_time", "25:99")

    def test_notify_mode_allowlist(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        update_config_field(p, "notify.mode", "telegram")
        body = p.read_text()
        assert 'mode = "telegram"' in body

    def test_notify_mode_invalid(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path)
        with pytest.raises(ConfigSetError, match="stdout"):
            update_config_field(p, "notify.mode", "discord")

    def test_comments_preserved(self, tmp_path: Path) -> None:
        """tomlkit must preserve the top-level comment after a set."""
        p = self._write_toml(tmp_path)
        update_config_field(p, "prep.prep_minutes", "25")
        body = p.read_text()
        assert "# Top-level comment that must survive" in body

    def test_allowlist_includes_expected_keys(self) -> None:
        for key in [
            "prep.prep_minutes",
            "prep.safety_buffer_minutes",
            "scheduling.morning_run_time",
            "scheduling.poll_interval_seconds",
            "scheduling.quiet_hours_start",
            "scheduling.quiet_hours_end",
            "notify.mode",
        ]:
            assert key in CONFIG_SET_ALLOWLIST


# ─────────── Pydantic field validation ─────────────────────────────────────────


class TestPydanticValidation:
    """Round-trip cases for the new Field(ge/le) and field_validator constraints."""

    def test_origin_rejects_swapped_or_oob_coords(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import Origin

        with pytest.raises(ValidationError):
            Origin(address="x", lat=200.0, lon=0.0)  # lat > 90
        with pytest.raises(ValidationError):
            Origin(address="x", lat=0.0, lon=-200.0)  # lon < -180

    def test_prep_rejects_negative_buffer(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import PrepConfig

        with pytest.raises(ValidationError):
            PrepConfig(prep_minutes=-1, safety_buffer_minutes=5)
        with pytest.raises(ValidationError):
            PrepConfig(prep_minutes=20, safety_buffer_minutes=-30)

    def test_scheduling_rejects_zero_poll_interval(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import SchedulingConfig

        with pytest.raises(ValidationError):
            SchedulingConfig(poll_interval_seconds=0)
        # Sensible value still works
        SchedulingConfig(poll_interval_seconds=60)

    def test_ha_base_url_must_be_http(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import HomeAssistantConfig

        with pytest.raises(ValidationError):
            HomeAssistantConfig(base_url="ftp://ha.example.com")
        # Trailing slash is stripped to keep URL joins clean.
        ha = HomeAssistantConfig(base_url="https://ha.example.com/")
        assert ha.base_url == "https://ha.example.com"

    def test_ha_base_url_empty_is_allowed(self) -> None:
        """Empty base_url is fine when home_assistant is disabled."""
        from commutecompass.config import HomeAssistantConfig

        ha = HomeAssistantConfig()
        assert ha.base_url == ""


# ── delete_config_field / list_overridden_allowlist_keys (`config unset`/`reset`) ──


class TestDeleteConfigField:
    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(body)
        return p

    def test_delete_removes_leaf_and_keeps_siblings(self, tmp_path: Path) -> None:
        from commutecompass.config import delete_config_field

        p = self._write(
            tmp_path,
            """\
[scheduling]
quiet_hours_start = "22:00"
quiet_hours_end = "07:00"
""",
        )
        removed = delete_config_field(p, "scheduling.quiet_hours_start")
        assert removed is True
        body = p.read_text()
        assert "quiet_hours_start" not in body
        assert "quiet_hours_end" in body

    def test_delete_absent_key_returns_false(self, tmp_path: Path) -> None:
        from commutecompass.config import delete_config_field

        p = self._write(tmp_path, "[scheduling]\n")
        assert delete_config_field(p, "scheduling.quiet_hours_start") is False

    def test_delete_unknown_key_raises(self, tmp_path: Path) -> None:
        from commutecompass.config import ConfigSetError, delete_config_field

        p = self._write(tmp_path, "[scheduling]\n")
        with pytest.raises(ConfigSetError):
            delete_config_field(p, "telegram_bot_token")

    def test_delete_nested_key_walks_subsections(self, tmp_path: Path) -> None:
        """Three-level keys like ``home_assistant.alarm.enabled`` walk subsections."""
        from commutecompass.config import delete_config_field

        p = self._write(
            tmp_path,
            """\
[home_assistant.alarm]
enabled = true
service = "script.boom"
""",
        )
        assert delete_config_field(p, "home_assistant.alarm.enabled") is True
        body = p.read_text()
        assert "enabled" not in body
        # Sibling field on the same subtable must be preserved.
        assert "script.boom" in body


class TestListOverriddenAllowlistKeys:
    def test_returns_only_allowlisted_keys_that_are_present(
        self, tmp_path: Path
    ) -> None:
        from commutecompass.config import list_overridden_allowlist_keys

        p = tmp_path / "config.toml"
        p.write_text(
            """\
[prep]
prep_minutes = 30

[scheduling]
quiet_hours_start = "22:00"
""",
        )
        present = list_overridden_allowlist_keys(p)
        assert set(present) == {"prep.prep_minutes", "scheduling.quiet_hours_start"}

    def test_returns_empty_when_no_overrides(self, tmp_path: Path) -> None:
        from commutecompass.config import list_overridden_allowlist_keys

        p = tmp_path / "config.toml"
        p.write_text("[other]\nthing = 1\n")
        assert list_overridden_allowlist_keys(p) == []


class TestExpandedAllowlist:
    """The HA toggle keys added for OpenClaw chat tweaks must round-trip."""

    def _write(self, tmp_path: Path) -> Path:
        p = _write_minimal_toml(tmp_path)
        p.write_text(p.read_text() + "\n[home_assistant]\n")
        return p

    def test_ha_replan_window_int(self, tmp_path: Path) -> None:
        from commutecompass.config import update_config_field

        p = self._write(tmp_path)
        assert update_config_field(p, "home_assistant.replan_window_minutes", "45") == 45
        body = p.read_text()
        assert "replan_window_minutes = 45" in body

    def test_ha_alarm_enabled_bool(self, tmp_path: Path) -> None:
        """Nested 3-level key writes into ``[home_assistant.alarm]``."""
        from commutecompass.config import update_config_field

        p = self._write(tmp_path)
        assert update_config_field(p, "home_assistant.alarm.enabled", "true") is True
        body = p.read_text()
        assert "enabled = true" in body
# ─────────── [alarm] section ───────────────────────────────────────────────────

class TestAlarmConfig:
    def test_defaults(self) -> None:
        from commutecompass.config import AlarmConfig

        a = AlarmConfig()
        assert a.enabled is False
        assert a.fallback_time == "12:00"
        assert a.wake_lead_minutes == 0
        assert a.sink == "analog"
        assert a.snooze_minutes == 9
        assert a.max_snoozes == 2
        assert a.max_ring_minutes == 15
        assert a.poll_seconds == 30
        assert a.publish_script == "script.commute_publish_alarm"

    def test_fallback_time_validated(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import AlarmConfig

        assert AlarmConfig(fallback_time="9:05").fallback_time == "09:05"
        with pytest.raises(ValidationError):
            AlarmConfig(fallback_time="noon")
        with pytest.raises(ValidationError):
            AlarmConfig(fallback_time="25:00")

    def test_window_defaults(self) -> None:
        from commutecompass.config import AlarmConfig

        w = AlarmConfig().window
        assert w.enabled is True
        assert w.first_alarm_lead_minutes == 37
        assert w.deadline_lead_minutes == 0
        assert w.snooze_minutes == 5
        assert (w.rering_start_percent, w.rering_hold_seconds, w.rering_ramp_seconds) == (
            20, 30, 60,
        )
        assert w.deadline_utc_entity == "input_text.commute_alarm_deadline_utc"
        assert w.window_done_entity == "input_text.commute_alarm_window_done_utc"

    def test_window_loads_from_toml_table(
        self, tmp_path: Path, minimal_toml: Path, required_env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for key, val in required_env.items():
            monkeypatch.setenv(key, val)
        p = tmp_path / "w.toml"
        p.write_text(
            minimal_toml.read_text()
            + "\n[alarm]\nenabled = true\n\n[alarm.window]\nsnooze_minutes = 4\n"
        )
        cfg = load_config(p)
        assert cfg.alarm.window.snooze_minutes == 4
        assert cfg.alarm.window.first_alarm_lead_minutes == 37
        assert cfg.alarm.window.deadline_lead_minutes == 0
        assert cfg.alarm.window.deadline_utc_entity == "input_text.commute_alarm_deadline_utc"

    def test_schedule_contract_defaults_legacy(self) -> None:
        from commutecompass.config import AlarmConfig

        a = AlarmConfig()
        assert a.schedule_contract == "legacy"
        assert a.schedule_entity == "input_text.commute_alarm_schedule_v1"
        assert a.contract_entity == "input_select.commute_alarm_contract"
        assert a.publish_v1_script == "script.commute_publish_alarm_v1"
        # Status export is opt-in: empty by default.
        assert a.status_entity == ""
        assert (
            AlarmConfig(status_entity="sensor.commute_alarm_status").status_entity
            == "sensor.commute_alarm_status"
        )

    def test_schedule_contract_validated(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import AlarmConfig

        assert AlarmConfig(schedule_contract="v1").schedule_contract == "v1"
        with pytest.raises(ValidationError):
            AlarmConfig(schedule_contract="v2")  # type: ignore[arg-type]

    def test_schedule_contract_loads_from_toml(
        self, tmp_path: Path, minimal_toml: Path, required_env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for k, v in required_env.items():
            monkeypatch.setenv(k, v)
        p = tmp_path / "c.toml"
        p.write_text(
            minimal_toml.read_text()
            + '\n[alarm]\nenabled = true\nschedule_contract = "v1"\n'
            'status_entity = "sensor.commute_alarm_status"\n'
        )
        cfg = load_config(p)
        assert cfg.alarm.schedule_contract == "v1"
        assert cfg.alarm.status_entity == "sensor.commute_alarm_status"

    def test_window_deadline_must_precede_first_alarm(self) -> None:
        from pydantic import ValidationError

        from commutecompass.config import AlarmWindowConfig

        with pytest.raises(ValidationError):
            AlarmWindowConfig(first_alarm_lead_minutes=13, deadline_lead_minutes=13)

    def test_alarm_keys_allowlisted(self) -> None:
        assert "alarm.fallback_time" in CONFIG_SET_ALLOWLIST
        assert "alarm.wake_lead_minutes" in CONFIG_SET_ALLOWLIST


class TestGtfsDbPath:
    def test_defaults_next_to_db_path(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        _apply_env(required_env)
        cfg = load_config(minimal_toml)
        assert cfg.paths.gtfs_db_path == str(
            Path(cfg.paths.db_path).with_name("gtfs.db")
        )

    def test_explicit_override(
        self, minimal_toml: Path, required_env: dict[str, str], tmp_path: Path
    ) -> None:
        _apply_env(required_env)
        text = minimal_toml.read_text().replace(
            "[paths]\n", f'[paths]\ngtfs_db_path = "{tmp_path / "x.db"}"\n', 1
        )
        minimal_toml.write_text(text)
        cfg = load_config(minimal_toml)
        assert cfg.paths.gtfs_db_path == str(tmp_path / "x.db")

    @pytest.mark.parametrize("explicit", [False, True])
    def test_loader_rejects_state_db_collision(
        self, minimal_toml: Path, required_env: dict[str, str], tmp_path: Path, explicit: bool
    ) -> None:
        from pydantic import ValidationError

        _apply_env(required_env)
        state_db = tmp_path / "gtfs.db"
        text = minimal_toml.read_text().replace(
            'db_path = "/var/lib/commutecompass/state.db"', f'db_path = "{state_db}"'
        )
        if explicit:
            text = text.replace("[paths]\n", f'[paths]\ngtfs_db_path = "{state_db}"\n')
        minimal_toml.write_text(text)
        with pytest.raises(ValidationError, match="gtfs_db_path must be separate from paths.db_path"):
            load_config(minimal_toml)
        assert not state_db.exists()

    @pytest.mark.parametrize(
        "alias", ["relative", "dotdot", "symlink", "parent_symlink", "symlink_dotdot", "hardlink"]
    )
    def test_rejects_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
    ) -> None:
        from pydantic import ValidationError

        from commutecompass.config import PathsConfig

        monkeypatch.chdir(tmp_path)
        state_db = tmp_path / "state.db"
        state_db.write_bytes(b"state database")
        gtfs_db = tmp_path / "alias.db"
        if alias == "relative":
            gtfs_db = Path("state.db")
        elif alias == "dotdot":
            (tmp_path / "child").mkdir()
            gtfs_db = tmp_path / "child" / ".." / "state.db"
        elif alias == "symlink":
            gtfs_db.symlink_to(state_db)
        elif alias == "parent_symlink":
            (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
            gtfs_db = tmp_path / "link" / "state.db"
        elif alias == "symlink_dotdot":
            # Resolve symlinks before '..', not just lexical normalization.
            (tmp_path / "nested" / "child").mkdir(parents=True)
            (tmp_path / "link").symlink_to(tmp_path / "nested" / "child")
            state_db = tmp_path / "nested" / "state.db"
            state_db.write_bytes(b"state database")
            gtfs_db = tmp_path / "link" / ".." / "state.db"
        else:
            gtfs_db.hardlink_to(state_db)
        with pytest.raises(ValidationError, match="gtfs_db_path must be separate"):
            PathsConfig(
                venues_file="venues.yaml", oauth_token_path="token.json",
                db_path=str(state_db), gtfs_db_path=str(gtfs_db),
            )
        assert state_db.read_bytes() == b"state database"

    @pytest.mark.parametrize("existing", [False, True])
    def test_accepts_distinct_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
    ) -> None:
        from commutecompass.config import PathsConfig

        monkeypatch.chdir(tmp_path)
        state_db = tmp_path / "state.db"
        gtfs_db = tmp_path / "index.db"
        if existing:
            state_db.write_bytes(b"same bytes, different inode")
            gtfs_db.write_bytes(state_db.read_bytes())
        cfg = PathsConfig(
            venues_file="venues.yaml", oauth_token_path="token.json",
            db_path=str(state_db), gtfs_db_path="index.db",
        )
        assert cfg.gtfs_db_path == "index.db"
        cfg.ensure_distinct_db_paths()
