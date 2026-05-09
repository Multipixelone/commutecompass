"""Tests for config.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from commutecompass.config import (
    Config,
    ConfigError,
    load_config,
    load_from_env,
)


# ─────────── Fixtures ───────────

@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
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
        """Full TOML + full env produces a valid Config."""
        _apply_env(required_env)
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

    def test_missing_env_raises_before_parsing_toml(self, minimal_toml: Path) -> None:
        """With no env vars set, we get a ConfigError before touching the file."""
        _clear_env([
            "GOOGLE_MAPS_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "OPENCODE_GO_TOKEN",
        ])
        err = pytest.raises(ConfigError, load_config, minimal_toml)
        # All five missing vars should be reported
        assert len(err.value.missing) == 5

    def test_missing_env_aggregates_all(self, minimal_toml: Path, required_env: dict[str, str]) -> None:
        """Only TELEGRAM_CHAT_ID missing — error lists exactly that one."""
        _apply_env(required_env)
        _clear_env(["TELEGRAM_CHAT_ID"])
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
        finally:
            os.environ.pop("HOME_ASSISTANT_TOKEN", None)