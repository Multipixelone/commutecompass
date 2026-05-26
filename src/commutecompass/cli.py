"""CLI entry point — commutecompass operational commands."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import click

from commutecompass.config import ConfigError  # noqa: F401
from commutecompass.joblock import JobLock, LockHeld, lock_path_for
from commutecompass.models import CalendarSpec

if TYPE_CHECKING:
    from commutecompass.config import Config

# Default config path
_CONFIG_DEFAULT = "/etc/commutecompass/config.toml"

# Process exit codes (sysexits-inspired).  These let the OpenClaw skill — or
# any other agent caller — distinguish kinds of failure without parsing logs.
EXIT_OK = 0
EXIT_USAGE = 64       # bad CLI arguments
EXIT_NOT_FOUND = 65   # subject doesn't exist (event/plan)
EXIT_UNRESOLVED = 66  # data could not be resolved (location/route)
EXIT_TRANSIENT = 75   # transient failure (job lock held, external API down)
EXIT_CONFIG = 78      # config error

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
        sys.exit(EXIT_CONFIG)


def _run_with_job_lock(cfg: "Config", job_name: str, fn: "Callable[[], None]") -> None:
    """Wrap a job invocation in a non-blocking flock so morning/poll cannot overlap.

    If another process already holds the lock, exit ``EXIT_TRANSIENT`` so cron
    / systemd will simply retry next cycle without producing a hard failure.
    """
    lock = JobLock(lock_path_for(cfg.paths.db_path, job_name), job_name=job_name)
    try:
        with lock:
            fn()
    except LockHeld as exc:
        click.echo(f"{job_name}: {exc}", err=True)
        sys.exit(EXIT_TRANSIENT)


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

    _run_with_job_lock(cfg, "morning", lambda: morning_run(cfg))


# ─────────── poll ─────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def poll(ctx: click.Context) -> None:
    """Run poll loop job."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.jobs.poll import run as poll_run

    _run_with_job_lock(cfg, "poll", lambda: poll_run(cfg))


# ─────────── tomorrow ────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Plan tomorrow and print the earliest prep time, but skip the HA push.",
)
@click.pass_context
def tomorrow(ctx: click.Context, dry_run: bool) -> None:
    """Push tomorrow's earliest prep time to the configured HA script.

    Designed to run from a systemd timer in the evening (e.g. 21:00 NYC).
    Plans tomorrow's events, picks the earliest prep_at, and POSTs it to
    ``[home_assistant.tomorrow].script`` so an iOS Shortcut can poll HA
    and set an on-device wake alarm. Today's planned state is left alone.
    """
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.jobs.tomorrow import run as tomorrow_run

    chosen = tomorrow_run(cfg, dry_run=dry_run)
    if chosen is None:
        click.echo("No plan-able commutes tomorrow — nothing pushed.")
        return

    assert chosen.prep_at is not None
    suffix = " (dry-run — HA push skipped)" if dry_run else ""
    click.echo(
        f"Tomorrow earliest prep: {chosen.prep_at.isoformat()} "
        f"event={chosen.event.id} ({chosen.event.title}){suffix}"
    )


# ─────────── plan EVENT_ID ───────────────────────────────────────────────────


@cli.command()
@click.argument("event_id")
@click.option(
    "--here",
    is_flag=True,
    help="Use the latest stored current location as origin (regardless of staleness).",
)
@click.pass_context
def plan(ctx: click.Context, event_id: str, here: bool) -> None:
    """Replan a single event (debug)."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.calendar_client import CalendarClient
    from commutecompass.llm import OpencodeGoClient
    from commutecompass.models import Origin
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

    origin_override: Optional[Origin] = None
    if here:
        cl = store.get_current_location(max_age_minutes=None)
        if cl is None:
            click.echo("No stored current location. Run `poll` first or check Home Assistant.", err=True)
            sys.exit(1)
        origin_override = Origin(
            address=f"{cl.lat:.6f},{cl.lon:.6f}",
            lat=cl.lat,
            lon=cl.lon,
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

    from commutecompass.models import ZoneInfo

    ha_zones: dict[str, ZoneInfo] = {}
    if cfg.home_assistant.enabled:
        from commutecompass.ha_client import fetch_zones

        ha_zones = fetch_zones(
            cfg.home_assistant.base_url,
            cfg.home_assistant_token,
        )

    new_plan = plan_event(
        event=event,
        config=cfg,
        venues=venues,
        store=store,
        llm=llm,
        origin_override=origin_override,
        ha_zones=ha_zones,
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
    """Send a test message via the configured notifier (telegram or stdout)."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.notify import build_notifier

    notifier = build_notifier(cfg)

    ok = notifier.send("🟢 commutecompass is alive — test notification OK")
    if ok:
        click.echo(f"Test message emitted via {cfg.notify.mode} notifier.", err=True)
    else:
        click.echo("Failed to send test message (see stderr for logs).", err=True)
        sys.exit(1)


# ─────────── where ───────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def where(ctx: click.Context) -> None:
    """Print the latest stored current location and its age."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.store import Store
    from commutecompass.timeutil import now_nyc

    store = Store(Path(cfg.paths.db_path))
    cl = store.get_current_location(max_age_minutes=None)
    if cl is None:
        click.echo("No current location stored.")
        return

    age_seconds = int((now_nyc() - cl.captured_at).total_seconds())
    click.echo(f"lat={cl.lat:.6f} lon={cl.lon:.6f} zone={cl.zone or '-'} age={age_seconds}s source={cl.source}")


# ─────────── digest-preview ──────────────────────────────────────────────────


@cli.command(name="digest-preview")
@click.pass_context
def digest_preview(ctx: click.Context) -> None:
    """Print today's digest from cached plans without sending anything.

    Reads the plans already stored by the latest ``morning`` run and renders
    them via the same formatter the digest uses. No Telegram traffic, no
    re-planning, no API calls. Useful for chat queries ("what's on today?").
    """
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.format import format_digest
    from commutecompass.store import Store

    store = Store(Path(cfg.paths.db_path))
    plans = store.today_plans()
    click.echo(format_digest(plans, alerts=[]))


# ─────────── adjust EVENT_ID ─────────────────────────────────────────────────


@cli.command()
@click.argument("event_id")
@click.option(
    "--add-prep",
    type=int,
    required=True,
    help="Minutes to add to the prep buffer (negative shrinks it). "
    "Shifts prep_at earlier by this many minutes.",
)
@click.pass_context
def adjust(ctx: click.Context, event_id: str, add_prep: int) -> None:
    """Shift today's prep time for EVENT_ID by --add-prep minutes.

    Use case: "I need to shower before this event, add 45 min" →
    ``adjust <id> --add-prep 45``.  The existing poll cycle re-fires the
    rescheduled prep ping at its new time.
    """
    import uuid
    from datetime import timedelta

    from commutecompass.format import format_prep_ping
    from commutecompass.models import PingEntry
    from commutecompass.store import Store
    from commutecompass.timeutil import now_nyc

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    store = Store(Path(cfg.paths.db_path))
    plan = store.get_plan(event_id)
    if plan is None:
        click.echo(f"No plan found for event {event_id}.", err=True)
        sys.exit(1)
    if plan.prep_at is None or plan.leave_at is None:
        click.echo(
            f"Event {event_id} has no scheduled prep/leave time — cannot adjust.",
            err=True,
        )
        sys.exit(1)

    now = now_nyc()
    new_prep_at = plan.prep_at - timedelta(minutes=add_prep)
    if new_prep_at < now:
        new_prep_at = now
    plan.prep_at = new_prep_at

    store.upsert_plan(plan)

    # Re-schedule the prep ping at the new time. schedule_ping replaces any
    # existing unfired prep ping for this event atomically.
    if new_prep_at > now:
        store.schedule_ping(
            PingEntry(
                id=str(uuid.uuid4()),
                event_id=event_id,
                kind="prep",
                fire_at=new_prep_at,
                fired=False,
                message=format_prep_ping(plan),
            )
        )

    direction = "earlier" if add_prep > 0 else "later"
    click.echo(
        f"Adjusted prep for {event_id}: prep_at {new_prep_at.strftime('%-I:%M %p')} "
        f"({abs(add_prep)} min {direction}); leave_at "
        f"{plan.leave_at.strftime('%-I:%M %p')} unchanged."
    )


# ─────────── config (group) ──────────────────────────────────────────────────


@cli.group()
def config() -> None:
    """View or edit allowlisted config fields."""


@config.command(name="show")
@click.option("--json", "as_json", is_flag=True, help="Emit pretty JSON instead of TOML.")
@click.pass_context
def config_show(ctx: click.Context, as_json: bool) -> None:
    """Print the effective config with secrets redacted."""
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.config import render_config_json, render_config_toml

    if as_json:
        click.echo(render_config_json(cfg))
    else:
        click.echo(render_config_toml(cfg))


@config.command(name="set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set an allowlisted config field. KEY uses dotted form (e.g. prep.prep_minutes)."""
    from commutecompass.config import ConfigSetError, update_config_field

    config_path: Path = ctx.obj["config_path"]
    try:
        coerced = update_config_field(config_path, key, value)
    except ConfigSetError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except OSError as exc:
        click.echo(f"Error writing {config_path}: {exc}", err=True)
        sys.exit(1)
    click.echo(f"{key} = {coerced!r}")


# ─────────── bot (stub) ──────────────────────────────────────────────────────


@cli.command()
def bot() -> None:
    """Telegram bot mode (stub)."""
    click.echo("Telegram bot mode is not yet implemented.")


# ─────────── entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point registered as the `commutecompass` console script."""
    cli()
