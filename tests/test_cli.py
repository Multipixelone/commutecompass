"""Tests for cli.py — command invocation and help text."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from commutecompass.cli import cli


# ─────────── Fixtures ─────────────────────────────────────────────────────────


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


@pytest.fixture
def env_block(required_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in required_env.items():
        monkeypatch.setenv(key, val)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ─────────── Help text tests ──────────────────────────────────────────────────


class TestMainHelp:
    def test_global_help_lists_commands(self, runner: CliRunner) -> None:
        """--help shows all subcommands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ["oauth", "init-db", "morning", "poll", "plan", "test-notify", "bot"]:
            assert cmd in result.output

    def test_global_help_shows_config_option(self, runner: CliRunner) -> None:
        """--help documents --config PATH."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "--config" in result.output

    def test_global_help_shows_default_config_path(self, runner: CliRunner) -> None:
        """--help shows the default config path."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # The default is /etc/commutecompass/config.toml
        assert "/etc/commutecompass/config.toml" in result.output


# ─────────── Command help tests ───────────────────────────────────────────────


class TestCommandHelp:
    def test_oauth_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["oauth", "--help"])
        assert result.exit_code == 0
        assert "OAuth" in result.output or "oauth" in result.output.lower()

    def test_init_db_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["init-db", "--help"])
        assert result.exit_code == 0
        assert "init" in result.output.lower() or "database" in result.output.lower()

    def test_morning_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["morning", "--help"])
        assert result.exit_code == 0
        assert "morning" in result.output.lower()

    def test_poll_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["poll", "--help"])
        assert result.exit_code == 0
        assert "poll" in result.output.lower()

    def test_plan_help_requires_event_id(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["plan", "--help"])
        assert result.exit_code == 0
        # Should show EVENT_ID argument
        assert "EVENT_ID" in result.output

    def test_test_notify_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["test-notify", "--help"])
        assert result.exit_code == 0
        assert "test" in result.output.lower() or "notify" in result.output.lower()

    def test_bot_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["bot", "--help"])
        assert result.exit_code == 0
        assert "bot" in result.output.lower()


# ─────────── bot stub ──────────────────────────────────────────────────────────


class TestBotStub:
    def test_bot_prints_not_yet_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["bot"])
        assert result.exit_code == 0
        assert "not yet implemented" in result.output.lower()


# ─────────── --config override ───────────────────────────────────────────────


class TestConfigOption:
    def test_custom_config_path_accepted(self, runner: CliRunner, minimal_toml: Path, env_block: None) -> None:
        """A custom --config path is accepted without error."""
        result = runner.invoke(
            cli,
            ["--config", str(minimal_toml), "init-db", "--help"],
        )
        # Should not fail on config loading during --help
        assert result.exit_code == 0


# ─────────── init-db wires correctly ────────────────────────────────────────


class TestInitDb:
    def test_init_db_creates_schema(
        self,
        runner: CliRunner,
        minimal_toml: Path,
        env_block: None,
        tmp_path: Path,
    ) -> None:
        """init-db creates the database file and schema."""
        db_path = tmp_path / "test.db"

        with mock.patch("commutecompass.config.load_config") as mock_cfg:
            from commutecompass.config import Config

            cfg = Config(
                origin={
                    "address": "123 Example Ave",
                    "lat": 40.6950,
                    "lon": -73.9890,
                    "subway_station": "Jay St-MetroTech",
                    "lirr_station": "Atlantic Terminal",
                },
                calendars=[],
                prep={"prep_minutes": 20, "safety_buffer_minutes": 5},
                scheduling={},
                paths={
                    "venues_file": str(tmp_path / "venues.yaml"),
                    "db_path": str(db_path),
                    "oauth_token_path": str(tmp_path / "token.json"),
                },
                opencode_go={"endpoint": "https://example.com"},
                mta={
                    "subway_alerts_url": "https://example.com/s",
                    "lirr_alerts_url": "https://example.com/l",
                    "bus_alerts_url": "https://example.com/b",
                },
                google_maps_api_key="fake",
                google_oauth_client_secret_json="{}",
                telegram_bot_token="123:abc",
                telegram_chat_id=-987654321,
                opencode_go_token="fake",
            )
            mock_cfg.return_value = cfg

            result = runner.invoke(cli, ["init-db"])
            assert result.exit_code == 0, result.output
            assert db_path.exists()


# ─────────── plan command ──────────────────────────────────────────────────────


class TestPlanCommand:
    def test_plan_requires_event_id(
        self, runner: CliRunner, minimal_toml: Path, env_block: None
    ) -> None:
        """plan without EVENT_ID argument shows usage error."""
        result = runner.invoke(cli, ["--config", str(minimal_toml), "plan"])
        assert result.exit_code != 0
        # Click should report missing argument
        assert "EVENT_ID" in result.output or "argument" in result.output.lower()


# ─────────── test-notify wires correctly ─────────────────────────────────────


class TestTestNotify:
    def test_test_notify_accepts_config(
        self, runner: CliRunner, minimal_toml: Path, env_block: None
    ) -> None:
        """test-notify --help works (verifies command wires config)."""
        result = runner.invoke(cli, ["--config", str(minimal_toml), "test-notify", "--help"])
        assert result.exit_code == 0


# ─────────── oauth wires correctly ─────────────────────────────────────────────


class TestOauthCommand:
    def test_oauth_accepts_config(
        self, runner: CliRunner, minimal_toml: Path, env_block: None
    ) -> None:
        """oauth --help works (verifies command wires config)."""
        result = runner.invoke(cli, ["--config", str(minimal_toml), "oauth", "--help"])
        assert result.exit_code == 0
