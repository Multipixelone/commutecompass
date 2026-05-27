"""Tests for cli.py — command invocation and help text."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from commutecompass.cli import cli
from commutecompass.config import Config
from commutecompass.models import Plan
from commutecompass.store import Store


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
        for cmd in [
            "oauth",
            "init-db",
            "morning",
            "poll",
            "plan",
            "test-notify",
            "bot",
            "digest-preview",
            "adjust",
            "config",
        ]:
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
        # `plan` now takes a SELECTOR (next / today:N / id / fuzzy title).
        assert "SELECTOR" in result.output

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
            from commutecompass.config import Config, Origin, PrepConfig, SchedulingConfig, PathsConfig, OpencodeGoConfig, MtaConfig

            cfg = Config(
                origin=Origin(
                    address="123 Example Ave",
                    lat=40.6950,
                    lon=-73.9890,
                    subway_station="Jay St-MetroTech",
                    lirr_station="Atlantic Terminal",
                ),
                calendars=[],
                prep=PrepConfig(prep_minutes=20, safety_buffer_minutes=5),
                scheduling=SchedulingConfig(),
                paths=PathsConfig(
                    venues_file=str(tmp_path / "venues.yaml"),
                    db_path=str(db_path),
                    oauth_token_path=str(tmp_path / "token.json"),
                ),
                opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
                mta=MtaConfig(
                    subway_alerts_url="https://example.com/s",
                    lirr_alerts_url="https://example.com/l",
                    bus_alerts_url="https://example.com/b",
                ),
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


# ─────────── digest-preview ──────────────────────────────────────────────────


def _fake_config(tmp_path: Path) -> Config:
    """Build a minimal Config object for cli tests that bypass load_config."""
    from commutecompass.config import (
        Config,
        MtaConfig,
        NotifyConfig,
        OpencodeGoConfig,
        Origin,
        PathsConfig,
        PrepConfig,
        SchedulingConfig,
    )

    return Config(
        origin=Origin(address="x", lat=0.0, lon=0.0),
        calendars=[],
        prep=PrepConfig(),
        scheduling=SchedulingConfig(),
        paths=PathsConfig(
            venues_file=str(tmp_path / "v.yaml"),
            db_path=str(tmp_path / "state.db"),
            oauth_token_path=str(tmp_path / "t.json"),
        ),
        opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
        mta=MtaConfig(
            subway_alerts_url="https://example.com/s",
            lirr_alerts_url="https://example.com/l",
            bus_alerts_url="https://example.com/b",
        ),
        notify=NotifyConfig(mode="stdout"),
    )


class TestDigestPreview:
    def test_no_plans_prints_no_events_message(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """With an empty DB, digest-preview prints a digest with the 'no events' line."""
        cfg = _fake_config(tmp_path)
        from commutecompass.store import Store

        Store(cfg.paths.db_path).init_schema()

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["digest-preview"])

        assert result.exit_code == 0, result.output
        assert "No events" in result.output or "Today" in result.output


# ─────────── adjust ──────────────────────────────────────────────────────────


class TestAdjustCommand:
    def _seed_plan(self, tmp_path: Path, prep_offset_min: int = 90, leave_offset_min: int = 60) -> tuple[Config, Store, Plan]:
        """Seed a plan in the DB with prep/leave times offset_min minutes in the future."""
        from datetime import timedelta

        from commutecompass.models import Event, Plan
        from commutecompass.store import Store
        from commutecompass.timeutil import now_nyc

        cfg = _fake_config(tmp_path)
        store = Store(cfg.paths.db_path)
        store.init_schema()
        now = now_nyc()
        event = Event(
            id="evt-abc",
            calendar_id="cal-1",
            calendar_name="Job",
            title="Standup",
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=3),
        )
        plan = Plan(
            event=event,
            leave_at=now + timedelta(minutes=leave_offset_min),
            prep_at=now + timedelta(minutes=prep_offset_min),
        )
        store.upsert_plan(plan)
        return cfg, store, plan

    def test_adjust_shifts_prep_earlier(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg, store, plan = self._seed_plan(tmp_path, prep_offset_min=90)
        original_prep = plan.prep_at

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "45"])

        assert result.exit_code == 0, result.output
        saved = store.get_plan("evt-abc")
        assert saved is not None and saved.prep_at is not None and original_prep is not None
        # Prep should have moved 45 minutes earlier
        delta = (original_prep - saved.prep_at).total_seconds() / 60
        assert 44 < delta < 46

    def test_adjust_clamps_to_now(self, runner: CliRunner, tmp_path: Path) -> None:
        """Negative add-prep that pushes prep_at below now is clamped to now."""
        from commutecompass.timeutil import now_nyc

        cfg, store, _ = self._seed_plan(tmp_path, prep_offset_min=10)

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "120"])

        assert result.exit_code == 0
        saved = store.get_plan("evt-abc")
        assert saved is not None and saved.prep_at is not None
        # Clamped to now ± a couple seconds; never in the past
        assert saved.prep_at >= now_nyc() - __import__("datetime").timedelta(seconds=2)

    def test_adjust_missing_event_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = _fake_config(tmp_path)
        from commutecompass.store import Store

        Store(cfg.paths.db_path).init_schema()

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["adjust", "nonexistent", "--add-prep", "10"])

        assert result.exit_code != 0
        assert "No plan found" in result.output

    def test_adjust_reschedules_prep_ping(self, runner: CliRunner, tmp_path: Path) -> None:
        """After adjust, a pending prep ping exists at the new fire_at."""
        from datetime import timedelta

        from commutecompass.timeutil import now_nyc

        cfg, store, plan = self._seed_plan(tmp_path, prep_offset_min=90)

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "30"])
        assert result.exit_code == 0

        # Query pending pings up to 2 hours from now
        pending = store.pending_pings(now_nyc() + timedelta(hours=2))
        prep_pings = [p for p in pending if p.kind == "prep" and p.event_id == "evt-abc"]
        assert len(prep_pings) == 1


# ─────────── config show / config set ────────────────────────────────────────


class TestConfigShow:
    def test_config_show_outputs_toml_by_default(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = _fake_config(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        # TOML-ish: contains "prep" header and an int assignment
        assert "[prep]" in result.output or "prep_minutes" in result.output

    def test_config_show_json_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = _fake_config(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["config", "show", "--json"])
        assert result.exit_code == 0
        import json as _json

        parsed = _json.loads(result.output)
        assert "prep" in parsed

    def test_config_show_redacts_secrets(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = _fake_config(tmp_path)
        cfg.telegram_bot_token = "supersecret"
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "supersecret" not in result.output
        assert "REDACTED" in result.output


class TestConfigSet:
    def _toml_with_prep(self, tmp_path: Path) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(
            """\
[prep]
prep_minutes = 20
safety_buffer_minutes = 5
"""
        )
        return p

    def test_set_allowed_key(self, runner: CliRunner, tmp_path: Path) -> None:
        p = self._toml_with_prep(tmp_path)
        result = runner.invoke(cli, ["--config", str(p), "config", "set", "prep.prep_minutes", "33"])
        assert result.exit_code == 0, result.output
        assert "prep_minutes = 33" in p.read_text()

    def test_set_disallowed_key_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        p = self._toml_with_prep(tmp_path)
        result = runner.invoke(
            cli, ["--config", str(p), "config", "set", "telegram_bot_token", "x"]
        )
        assert result.exit_code != 0
        # Error message should name the allowlist
        assert "prep.prep_minutes" in result.output


# ─────────── adjust idempotency ────────────────────────────────────────────────


class TestAdjustIdempotency:
    """The --idempotency-key flag prevents stacked offsets on retried calls."""

    def _config_for_tmp(self, tmp_path: Path) -> Config:
        from commutecompass.config import (
            Config,
            MtaConfig,
            OpencodeGoConfig,
            Origin,
            PathsConfig,
            PrepConfig,
            SchedulingConfig,
        )

        return Config(
            origin=Origin(address="x", lat=40.7, lon=-74.0),
            calendars=[],
            prep=PrepConfig(),
            scheduling=SchedulingConfig(),
            paths=PathsConfig(
                venues_file=str(tmp_path / "venues.yaml"),
                db_path=str(tmp_path / "state.db"),
                oauth_token_path=str(tmp_path / "token.json"),
            ),
            opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
            mta=MtaConfig(
                subway_alerts_url="https://example.com/s",
                lirr_alerts_url="https://example.com/l",
                bus_alerts_url="https://example.com/b",
            ),
            google_maps_api_key="x",
            google_oauth_client_secret_json="{}",
            telegram_bot_token="123:abc",
            telegram_chat_id=-1,
            opencode_go_token="x",
        )

    def _seed_plan(self, cfg: Config) -> str:
        """Create a today plan and return its event id."""
        from datetime import timedelta

        from commutecompass.models import Event, Plan
        from commutecompass.store import Store
        from commutecompass.timeutil import now_nyc

        store = Store(cfg.paths.db_path)
        store.init_schema()

        now = now_nyc()
        start = now + timedelta(hours=4)
        event = Event(
            id="evt-adjust-idem",
            calendar_id="c", calendar_name="C",
            title="Adjustable", start=start, end=start + timedelta(hours=1),
        )
        plan = Plan(
            event=event,
            leave_at=start - timedelta(minutes=45),
            prep_at=start - timedelta(minutes=65),
        )
        store.upsert_plan(plan)
        return event.id

    def test_idempotency_key_makes_second_call_a_noop(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = self._config_for_tmp(tmp_path)
        event_id = self._seed_plan(cfg)

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            r1 = runner.invoke(
                cli,
                ["adjust", event_id, "--add-prep", "30", "--idempotency-key", "k1"],
            )
            assert r1.exit_code == 0, r1.output

            from commutecompass.store import Store
            store = Store(cfg.paths.db_path)
            after_first = store.get_plan(event_id)
            assert after_first is not None
            first_prep = after_first.prep_at

            r2 = runner.invoke(
                cli,
                ["adjust", event_id, "--add-prep", "30", "--idempotency-key", "k1"],
            )
            assert r2.exit_code == 0, r2.output
            assert "already applied" in r2.output.lower()

            after_second = store.get_plan(event_id)
            assert after_second is not None
            # prep_at must not have shifted on the retry.
            assert after_second.prep_at == first_prep

    def test_adjust_missing_event_returns_not_found_exit(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = self._config_for_tmp(tmp_path)
        from commutecompass.store import Store
        store = Store(cfg.paths.db_path)
        store.init_schema()

        from commutecompass.cli import EXIT_NOT_FOUND

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(
                cli, ["adjust", "does-not-exist", "--add-prep", "10"]
            )
        assert result.exit_code == EXIT_NOT_FOUND


# ─────────── status command ───────────────────────────────────────────────────


class TestStatusCommand:
    """`commutecompass status` returns a snapshot of today's state."""

    def _config(self, tmp_path: Path) -> Config:
        from commutecompass.config import (
            Config, MtaConfig, OpencodeGoConfig, Origin, PathsConfig,
            PrepConfig, SchedulingConfig,
        )

        return Config(
            origin=Origin(address="x", lat=40.7, lon=-74.0),
            calendars=[],
            prep=PrepConfig(),
            scheduling=SchedulingConfig(),
            paths=PathsConfig(
                venues_file=str(tmp_path / "venues.yaml"),
                db_path=str(tmp_path / "state.db"),
                oauth_token_path=str(tmp_path / "token.json"),
            ),
            opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
            mta=MtaConfig(
                subway_alerts_url="https://example.com/s",
                lirr_alerts_url="https://example.com/l",
                bus_alerts_url="https://example.com/b",
            ),
            google_maps_api_key="x",
            google_oauth_client_secret_json="{}",
            telegram_bot_token="123:abc",
            telegram_chat_id=-1,
            opencode_go_token="x",
        )

    def test_status_text_on_empty_db(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        from commutecompass.store import Store
        Store(cfg.paths.db_path).init_schema()

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "plans today: 0" in result.output
        assert "pings today: 0" in result.output
        assert "location: (none)" in result.output

    def test_status_json_shape(self, runner: CliRunner, tmp_path: Path) -> None:
        import json as _json

        cfg = self._config(tmp_path)
        from commutecompass.store import Store
        Store(cfg.paths.db_path).init_schema()

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert set(payload.keys()) >= {
            "now", "plans", "pings", "current_location", "geocode_cache",
        }
        assert payload["plans"] == []
        assert payload["pings"] == []


# ─────────── geocode-cache command ─────────────────────────────────────────────


class TestGeocodeCacheCommand:
    def _config(self, tmp_path: Path) -> Config:
        from commutecompass.config import (
            Config, MtaConfig, OpencodeGoConfig, Origin, PathsConfig,
            PrepConfig, SchedulingConfig,
        )

        return Config(
            origin=Origin(address="x", lat=40.7, lon=-74.0),
            calendars=[],
            prep=PrepConfig(),
            scheduling=SchedulingConfig(),
            paths=PathsConfig(
                venues_file=str(tmp_path / "venues.yaml"),
                db_path=str(tmp_path / "state.db"),
                oauth_token_path=str(tmp_path / "token.json"),
            ),
            opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
            mta=MtaConfig(
                subway_alerts_url="https://example.com/s",
                lirr_alerts_url="https://example.com/l",
                bus_alerts_url="https://example.com/b",
            ),
            google_maps_api_key="x",
            google_oauth_client_secret_json="{}",
            telegram_bot_token="123:abc",
            telegram_chat_id=-1,
            opencode_go_token="x",
        )

    def test_invalidate_existing_entry(self, runner: CliRunner, tmp_path: Path) -> None:
        from commutecompass.models import ResolvedLocation
        from commutecompass.store import Store

        cfg = self._config(tmp_path)
        store = Store(cfg.paths.db_path)
        store.init_schema()
        store.cache_geocode(
            "Old Workplace",
            ResolvedLocation(kind="address", value="x", lat=1.0, lon=2.0, source="geocode"),
        )

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            r = runner.invoke(
                cli, ["geocode-cache", "--invalidate", "Old Workplace"]
            )
        assert r.exit_code == 0, r.output
        assert "removed" in r.output.lower()
        # Entry is gone.
        assert store.get_geocode("Old Workplace") is None

    def test_invalidate_missing_entry_exits_not_found(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = self._config(tmp_path)
        from commutecompass.cli import EXIT_NOT_FOUND
        from commutecompass.store import Store

        Store(cfg.paths.db_path).init_schema()
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            r = runner.invoke(cli, ["geocode-cache", "--invalidate", "Nothing"])
        assert r.exit_code == EXIT_NOT_FOUND


# ─────────── snooze / mute / unmute / undo / mta-alerts ──────────────────────


def _seed_today_plan(
    tmp_path: Path,
    *,
    event_id: str = "evt-abc",
    title: str = "Standup",
) -> tuple[Config, Store, Plan]:
    """Seed a single today-plan with prep/leave that survive the 2am NYC boundary.

    The seeded ``event.start`` is anchored inside ``logical_day_bounds_nyc()``
    so ``today_plans()`` always returns it.  ``prep_at`` and ``leave_at`` are
    derived from ``event.start`` (clamped to be a few minutes after ``now``)
    so commands that operate on pending pings still find something pending.
    """
    from datetime import timedelta

    from commutecompass.models import Event, Plan as _Plan
    from commutecompass.timeutil import logical_day_bounds_nyc, now_nyc

    cfg = _fake_config(tmp_path)
    store = Store(cfg.paths.db_path)
    store.init_schema()

    now = now_nyc()
    day_start, day_end = logical_day_bounds_nyc()

    # Pick an event.start safely inside (now, day_end) — preferring mid-day
    # but clamping to day_end - 5min so the row stays in today's window.
    event_start = max(day_start + timedelta(hours=12), now + timedelta(minutes=90))
    event_start = min(event_start, day_end - timedelta(minutes=5))
    if event_start <= now + timedelta(minutes=15):
        pytest.skip("Test runs across the NYC logical-day boundary; rerun.")

    prep_at = max(event_start - timedelta(minutes=60), now + timedelta(minutes=5))
    leave_at = max(event_start - timedelta(minutes=30), now + timedelta(minutes=10))

    event = Event(
        id=event_id,
        calendar_id="cal-1",
        calendar_name="Job",
        title=title,
        start=event_start,
        end=event_start + timedelta(hours=1),
    )
    plan = _Plan(event=event, leave_at=leave_at, prep_at=prep_at)
    store.upsert_plan(plan)
    return cfg, store, plan


class TestSnoozeCommand:
    """`commutecompass snooze SELECTOR --minutes N | --skip` operates on prep pings."""

    def test_snooze_shifts_prep_ping_forward(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        from commutecompass.models import PingEntry
        from commutecompass.timeutil import now_nyc

        cfg, store, plan = _seed_today_plan(tmp_path)
        # Schedule a real pending prep ping at plan.prep_at.
        assert plan.prep_at is not None
        store.schedule_ping(
            PingEntry(
                id="ping-1",
                event_id="evt-abc",
                kind="prep",
                fire_at=plan.prep_at,
                fired=False,
                message="prep!",
            )
        )

        original_fire = plan.prep_at

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["snooze", "evt-abc", "--minutes", "20"])

        assert result.exit_code == 0, result.output
        pending = store.pending_pings(now_nyc() + timedelta(hours=4))
        prep = [p for p in pending if p.event_id == "evt-abc" and p.kind == "prep"]
        assert len(prep) == 1
        # The new fire_at is ~20 minutes after the original (allowing ms drift).
        delta = (prep[0].fire_at - original_fire).total_seconds() / 60
        assert 19.5 < delta < 20.5

    def test_snooze_skip_marks_ping_fired(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        from commutecompass.models import PingEntry
        from commutecompass.timeutil import now_nyc

        cfg, store, plan = _seed_today_plan(tmp_path)
        assert plan.prep_at is not None
        store.schedule_ping(
            PingEntry(
                id="ping-skip",
                event_id="evt-abc",
                kind="prep",
                fire_at=plan.prep_at,
                fired=False,
                message="prep!",
            )
        )

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["snooze", "evt-abc", "--skip"])

        assert result.exit_code == 0, result.output
        pending = store.pending_pings(now_nyc() + timedelta(hours=4))
        assert not any(p.event_id == "evt-abc" and p.kind == "prep" for p in pending)

    def test_snooze_requires_one_of_minutes_or_skip(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_USAGE

        cfg, _, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["snooze", "evt-abc"])
        assert result.exit_code == EXIT_USAGE

    def test_snooze_with_no_pending_ping_exits_not_found(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_NOT_FOUND

        # Seed a plan but no ping at all.
        cfg, _, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["snooze", "evt-abc", "--minutes", "10"])
        assert result.exit_code == EXIT_NOT_FOUND


class TestMuteCommand:
    """`mute SELECTOR` and `mute --today` suppress upcoming pings."""

    def test_mute_event_sets_mute_and_cancels_pings(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.models import PingEntry

        cfg, store, plan = _seed_today_plan(tmp_path)
        assert plan.prep_at is not None
        store.schedule_ping(
            PingEntry(
                id="p-1", event_id="evt-abc", kind="prep",
                fire_at=plan.prep_at, fired=False, message="prep",
            )
        )

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["mute", "evt-abc"])

        assert result.exit_code == 0, result.output
        assert store.is_muted("evt-abc") is True
        from datetime import timedelta
        from commutecompass.timeutil import now_nyc
        pending = store.pending_pings(now_nyc() + timedelta(hours=4))
        assert not any(p.event_id == "evt-abc" for p in pending)

    def test_mute_today_mutes_all_plans(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg, store, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["mute", "--today"])
        assert result.exit_code == 0, result.output
        assert store.is_muted("evt-abc") is True

    def test_mute_requires_selector_or_today_flag(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_USAGE

        cfg, _, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["mute"])
        assert result.exit_code == EXIT_USAGE


class TestUnmuteCommand:
    def test_unmute_lifts_mute(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg, store, _ = _seed_today_plan(tmp_path)
        store.mute_event("evt-abc")

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["unmute", "evt-abc"])

        assert result.exit_code == 0, result.output
        assert store.is_muted("evt-abc") is False


class TestUndoCommand:
    """`undo` restores prev_prep_at recorded in the adjust_log."""

    def test_undo_reverts_last_adjust(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg, store, plan = _seed_today_plan(tmp_path)
        assert plan.prep_at is not None
        original_prep = plan.prep_at

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            r1 = runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "30"])
            assert r1.exit_code == 0, r1.output

            after_adjust = store.get_plan("evt-abc")
            assert after_adjust is not None and after_adjust.prep_at is not None
            assert after_adjust.prep_at != original_prep

            r2 = runner.invoke(cli, ["undo", "evt-abc"])
            assert r2.exit_code == 0, r2.output

        restored = store.get_plan("evt-abc")
        assert restored is not None and restored.prep_at is not None
        # ISO round-trip preserves the value exactly (modulo microseconds).
        assert abs((restored.prep_at - original_prep).total_seconds()) < 1

    def test_undo_with_no_history_exits_not_found(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_NOT_FOUND

        cfg, _, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["undo", "evt-abc"])
        assert result.exit_code == EXIT_NOT_FOUND

    def test_undo_two_steps_walks_back_history(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Single-step undo + repeat: each call removes one row from history."""
        cfg, store, plan = _seed_today_plan(tmp_path)
        assert plan.prep_at is not None
        original_prep = plan.prep_at

        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "10"])
            after_first = store.get_plan("evt-abc")
            assert after_first is not None and after_first.prep_at is not None
            first_prep = after_first.prep_at

            runner.invoke(cli, ["adjust", "evt-abc", "--add-prep", "20"])

            # First undo should restore to first_prep, not to original.
            runner.invoke(cli, ["undo"])
            after_undo1 = store.get_plan("evt-abc")
            assert after_undo1 is not None and after_undo1.prep_at is not None
            assert abs((after_undo1.prep_at - first_prep).total_seconds()) < 1

            # Second undo should restore to original.
            runner.invoke(cli, ["undo"])
            after_undo2 = store.get_plan("evt-abc")
            assert after_undo2 is not None and after_undo2.prep_at is not None
            assert abs((after_undo2.prep_at - original_prep).total_seconds()) < 1


class TestMtaAlertsCommand:
    """`mta-alerts` filters fresh alerts against today's planned routes."""

    def test_mta_alerts_no_plans_message(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg = _fake_config(tmp_path)
        Store(cfg.paths.db_path).init_schema()
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(cli, ["mta-alerts"])
        assert result.exit_code == 0
        assert "nothing to filter" in result.output.lower() or "no planned" in result.output.lower()

    def test_mta_alerts_renders_affecting_alerts(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        from commutecompass.models import Alert, Event, Plan as _Plan, Route, TransitLeg
        from commutecompass.timeutil import now_nyc

        cfg = _fake_config(tmp_path)
        store = Store(cfg.paths.db_path)
        store.init_schema()
        now = now_nyc()
        depart = now + timedelta(minutes=30)
        arrive = depart + timedelta(minutes=20)
        leg = TransitLeg(
            mode="TRANSIT", system="MTA Subway", line="A",
            depart_at=depart, arrive_at=arrive, duration_seconds=1200,
            summary="A from X to Y",
        )
        route = Route(
            legs=[leg], depart_at=depart, arrive_at=arrive,
            total_duration_seconds=1200, transfers=0,
        )
        event = Event(
            id="evt-mta",
            calendar_id="cal", calendar_name="Cal", title="Show",
            start=arrive + timedelta(minutes=10), end=arrive + timedelta(hours=2),
        )
        store.upsert_plan(
            _Plan(event=event, route=route, leave_at=depart, prep_at=depart - timedelta(minutes=20))
        )

        alert = Alert(
            id="alert-A-1",
            header="A train delays",
            description="Signal problem on the A.",
            affected_routes={"A"},
            affected_systems={"MTA Subway"},
            active_periods=[(depart - timedelta(hours=1), depart + timedelta(hours=2))],
            severity="WARNING",
        )

        with mock.patch("commutecompass.config.load_config", return_value=cfg), \
            mock.patch("commutecompass.mta.fetch_alerts", return_value=[alert]):
            result = runner.invoke(cli, ["mta-alerts"])

        assert result.exit_code == 0, result.output
        assert "A train delays" in result.output
        assert "Active service alerts" in result.output


# ─────────── config unset / reset ─────────────────────────────────────────────


class TestConfigUnset:
    def _toml_with_quiet_hours(self, tmp_path: Path) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(
            """\
[scheduling]
quiet_hours_start = "22:00"
quiet_hours_end = "07:00"
"""
        )
        return p

    def test_unset_removes_key_from_toml(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        p = self._toml_with_quiet_hours(tmp_path)
        result = runner.invoke(
            cli, ["--config", str(p), "config", "unset", "scheduling.quiet_hours_start"]
        )
        assert result.exit_code == 0, result.output
        body = p.read_text()
        assert "quiet_hours_start" not in body
        assert "quiet_hours_end" in body  # other key untouched

    def test_unset_unknown_key_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_USAGE

        p = self._toml_with_quiet_hours(tmp_path)
        result = runner.invoke(
            cli, ["--config", str(p), "config", "unset", "secret.thing"]
        )
        assert result.exit_code == EXIT_USAGE

    def test_unset_idempotent_when_already_absent(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        p = tmp_path / "config.toml"
        p.write_text("[scheduling]\n")  # no key present
        result = runner.invoke(
            cli, ["--config", str(p), "config", "unset", "scheduling.quiet_hours_start"]
        )
        assert result.exit_code == 0
        assert "already unset" in result.output.lower() or "default" in result.output.lower()


class TestConfigReset:
    def _toml_with_overrides(self, tmp_path: Path) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(
            """\
[prep]
prep_minutes = 30

[scheduling]
quiet_hours_start = "22:00"
quiet_hours_end = "07:00"
"""
        )
        return p

    def test_reset_without_yes_lists_overrides_and_refuses(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_USAGE

        p = self._toml_with_overrides(tmp_path)
        result = runner.invoke(cli, ["--config", str(p), "config", "reset"])
        assert result.exit_code == EXIT_USAGE
        assert "prep.prep_minutes" in result.output
        # File unchanged.
        body = p.read_text()
        assert "prep_minutes = 30" in body

    def test_reset_with_yes_removes_all_allowlisted_overrides(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        p = self._toml_with_overrides(tmp_path)
        result = runner.invoke(
            cli, ["--config", str(p), "config", "reset", "--yes"]
        )
        assert result.exit_code == 0, result.output
        body = p.read_text()
        # All three allowlisted overrides should be gone.
        assert "prep_minutes" not in body
        assert "quiet_hours_start" not in body
        assert "quiet_hours_end" not in body


# ─────────── plan --from ──────────────────────────────────────────────────────


class TestPlanFromOption:
    """`plan SELECTOR --from <address>` is a preview that never saves."""

    def test_plan_help_lists_from_option(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["plan", "--help"])
        assert result.exit_code == 0
        assert "--from" in result.output
        assert "preview" in result.output.lower()

    def test_plan_here_and_from_are_mutually_exclusive(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from commutecompass.cli import EXIT_USAGE

        cfg, _, _ = _seed_today_plan(tmp_path)
        with mock.patch("commutecompass.config.load_config", return_value=cfg):
            result = runner.invoke(
                cli, ["plan", "evt-abc", "--here", "--from", "X"]
            )
        assert result.exit_code == EXIT_USAGE
        assert "mutually exclusive" in result.output.lower()
