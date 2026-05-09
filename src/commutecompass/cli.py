"""CLI entry point — commutecompass operational commands."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from commutecompass.config import ConfigError  # noqa: F401
from commutecompass.models import CalendarSpec

if TYPE_CHECKING:
    from commutecompass.config import Config

# Default config path
_CONFIG_DEFAULT = "/etc/commutecompass/config.toml"

_logger = logging.getLogger(__name__)


# ─────────── Config helper ────────────────────────────────────────────────────


def _load_config(config_path: Path) -> "Config":
    """Load and return the Config, exiting gracefully on error."""
    # Import module to allow patching at the right spot
    from commutecompass import config as config_mod

    try:
        return config_mod.load_config(config_path)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ─────────── Click group ──────────────────────────────────────────────────────


@click.group()
@click.option(
    "--config",
    type=click.Path(exists=False, path_type=Path),
    default=_CONFIG_DEFAULT,
    help="Path to config.toml",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, config: Path) -> None:
    """commutecompass — NYC commute orchestrator."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ─────────── oauth ────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def oauth(ctx: click.Context) -> None:
    """Interactive Google Calendar OAuth setup."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.calendar_client import CalendarClient

    token_path = Path(cfg.paths.oauth_token_path)
    click.echo(f"Starting OAuth flow... Token will be saved to {token_path}")
    click.echo("A browser window should open automatically.")

    client = CalendarClient(
        client_secret_json=cfg.google_oauth_client_secret_json,
        token_path=token_path,
    )
    client.authorize_interactive()
    click.echo("OAuth授权完成。Token已保存。")


# ─────────── init-db ──────────────────────────────────────────────────────────


@cli.command(name="init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Initialize SQLite database schema."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    db_path = Path(cfg.paths.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    from commutecompass.store import Store

    store = Store(db_path)
    store.init_schema()
    click.echo(f"Database schema initialized at {db_path}")


# ─────────── morning ──────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def morning(ctx: click.Context) -> None:
    """Run morning digest job."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.jobs.morning import run as morning_run

    morning_run(cfg)


# ─────────── poll ─────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def poll(ctx: click.Context) -> None:
    """Run poll loop job."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.jobs.poll import run as poll_run

    poll_run(cfg)


# ─────────── plan EVENT_ID ───────────────────────────────────────────────────


@cli.command()
@click.argument("event_id")
@click.pass_context
def plan(ctx: click.Context, event_id: str) -> None:
    """Replan a single event (debug)."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.calendar_client import CalendarClient
    from commutecompass.llm import OpencodeGoClient
    from commutecompass.planner import plan_event
    from commutecompass.store import Store
    from commutecompass.venues import VenueRegistry
    from commutecompass.timeutil import now_nyc

    token_path = Path(cfg.paths.oauth_token_path)
    cal_client = CalendarClient(
        client_secret_json=cfg.google_oauth_client_secret_json,
        token_path=token_path,
    )
    venues = VenueRegistry.load(Path(cfg.paths.venues_file))
    store = Store(Path(cfg.paths.db_path))
    llm = OpencodeGoClient(
        endpoint=cfg.opencode_go.endpoint,
        token=cfg.opencode_go_token,
        model=cfg.opencode_go.model,
    )

    # Fetch the event from the store first (today's planned event)
    existing = store.get_plan(event_id)
    if existing is None:
        click.echo(f"No plan found for event {event_id}. Trying to fetch from calendar...")
        now = now_nyc()
        events = cal_client.fetch_events(
            [CalendarSpec(id=cal.id, name=cal.name, enabled=cal.enabled) for cal in cfg.calendars],
            now,
            now,
        )
        if not events:
            click.echo(f"Event {event_id} not found in today's calendars.")
            sys.exit(1)
        event = events[0]
    else:
        event = existing.event

    new_plan = plan_event(
        event=event,
        config=cfg,
        venues=venues,
        store=store,
        llm=llm,
    )

    store.upsert_plan(new_plan)

    if new_plan.error:
        click.echo(f"Plan error: {new_plan.error}")
    else:
        assert new_plan.leave_at is not None
        assert new_plan.prep_at is not None
        click.echo(f"Plan updated for {event_id}:")
        click.echo(f"  Leave at:   {new_plan.leave_at.strftime('%H:%M:%S')}")
        click.echo(f"  Start prep: {new_plan.prep_at.strftime('%H:%M:%S')}")
        if new_plan.route:
            click.echo(f"  Duration:   {new_plan.route.total_duration_seconds // 60} min")


# ─────────── test-notify ─────────────────────────────────────────────────────


@cli.command(name="test-notify")
@click.pass_context
def test_notify(ctx: click.Context) -> None:
    """Send a test Telegram message."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.notify import TelegramNotifier

    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
    )

    ok = notifier.send("🟢 commutecompass is alive — test notification OK")
    if ok:
        click.echo("Test message sent successfully.")
    else:
        click.echo("Failed to send test message (see stderr for logs).", err=True)
        sys.exit(1)


# ─────────── bot (stub) ──────────────────────────────────────────────────────


@cli.command()
def bot() -> None:
    """Telegram bot mode (stub)."""
    click.echo("Telegram bot mode is not yet implemented.")


# ─────────── entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point registered as the `commutecompass` console script."""
    cli()
