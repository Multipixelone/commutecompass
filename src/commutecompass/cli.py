"""CLI entry point — commutecompass operational commands."""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import click
from pydantic import ValidationError

from commutecompass.config import ConfigError, format_validation_error
from commutecompass.joblock import JobLock, LockHeld, lock_path_for
from commutecompass.models import CalendarSpec

if TYPE_CHECKING:
    from commutecompass.config import Config
    from commutecompass.models import Route
    from commutecompass.store import Store

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


def _load_config(config_path: Path) -> Config:
    """Load and return the Config, exiting gracefully on error."""
    # Import module to allow patching at the right spot
    from commutecompass import config as config_mod

    try:
        return config_mod.load_config(config_path)
    except ValidationError as exc:
        click.echo(f"Error: {format_validation_error(exc)}", err=True)
        sys.exit(EXIT_CONFIG)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)


def _resolve_or_exit(selector: str, store: Store) -> str:
    """Resolve a selector via ``selector.resolve_event_selector`` or exit cleanly.

    Centralises the error → exit-code mapping so every event-scoped command
    (``adjust``, ``plan``, ``snooze``, ``mute``, ``undo``) reports selector
    failures the same way and an OpenClaw caller can pattern-match on it.
    """
    from commutecompass.selector import SelectorError, resolve_event_selector
    from commutecompass.timeutil import now_nyc

    try:
        return resolve_event_selector(selector, store, now=now_nyc())
    except SelectorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(exc.exit_code)


def _run_with_job_lock(cfg: Config, job_name: str, fn: Callable[[], None]) -> None:
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
    except sqlite3.OperationalError as exc:
        click.echo(f"{job_name}: database operation failed: {exc}", err=True)
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
    # Surface our own job-level INFO lines (e.g. "tomorrow job: fetched 0
    # events for …") to stderr so systemd's journal captures them. Without
    # this the root logger stays at WARNING and silent-success branches
    # are impossible to distinguish from real failures after the fact.
    # Root stays at WARNING so chatty deps (httpx/googleapiclient) don't
    # spam the journal — and crucially don't leak the Google Maps API key
    # via httpx INFO lines that log full request URLs.
    # COMMUTECOMPASS_LOG_LEVEL=DEBUG widens both on demand.
    import os as _os
    _level_name = _os.environ.get("COMMUTECOMPASS_LOG_LEVEL", "INFO").upper()
    _app_level = getattr(logging, _level_name, logging.INFO)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("commutecompass").setLevel(_app_level)

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
    click.echo("OAuth authorization complete. Token saved.")


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


# ─────────── alarm (wake & nap) ──────────────────────────────────────────────


def _alarm_ha_or_exit(cfg: Config) -> tuple[str, str]:
    """Return (base_url, token) or exit EXIT_CONFIG when HA isn't configured."""
    from commutecompass.alarm import AlarmConfigError, require_ha

    try:
        return require_ha(cfg)
    except AlarmConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)


def _alarm_call(cfg: Config, service: str, data: dict[str, object]) -> None:
    """Call ``domain.service`` on HA; exit EXIT_CONFIG/EXIT_TRANSIENT on failure."""
    from commutecompass.alarm import AlarmConfigError, split_service
    from commutecompass.ha_client import call_service

    base_url, token = _alarm_ha_or_exit(cfg)
    try:
        domain, name = split_service(service)
    except AlarmConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    if not call_service(base_url, token, domain, name, data):
        click.echo(f"Error: Home Assistant call {service} failed", err=True)
        sys.exit(EXIT_TRANSIENT)


@cli.group()
@click.pass_context
def alarm(ctx: click.Context) -> None:
    """Wake & nap alarm (publishes to Home Assistant; daemon rings link)."""
    cfg = _load_config(ctx.obj["config_path"])
    if not cfg.alarm.enabled:
        click.echo("Error: [alarm].enabled is false", err=True)
        sys.exit(EXIT_CONFIG)


@alarm.command(name="publish")
@click.option("--dry-run", is_flag=True, help="Compute and print the wake time; skip the HA push.")
@click.pass_context
def alarm_publish(ctx: click.Context, dry_run: bool) -> None:
    """Compute the next morning wake and publish it to HA."""
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_publish_result(cfg, dry_run=dry_run)


def _alarm_publish_result(cfg: Config, *, dry_run: bool = False) -> None:
    from commutecompass.alarm import AlarmConfigError, AlarmError, publish

    try:
        result, ok = publish(cfg, dry_run=dry_run)
    except AlarmConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    except AlarmError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_TRANSIENT)
    nxt = result.next_prep_at.isoformat() if result.next_prep_at else "-"
    suffix = " (dry-run — HA push skipped)" if dry_run else ""
    click.echo(f"Wake: {result.wake_at.isoformat()} ({result.source}) next_prep_at={nxt}{suffix}")
    if result.warning:
        click.echo(f"Warning: {result.warning}", err=True)
    if not dry_run and not ok:
        click.echo("Error: Home Assistant publish failed", err=True)
        sys.exit(EXIT_TRANSIENT)


@alarm.command(name="wake")
@click.option("--leave", help="Leave deadline in NYC time (HH:MM, 9am or 9:30am).")
@click.option("--date", "date_value", help="NYC deadline date (YYYY-MM-DD).")
@click.option("--force", is_flag=True, help="Select this deadline even over an earlier calendar wake.")
@click.option("--clear", is_flag=True, help="Clear the manual deadline and republish the normal wake.")
@click.pass_context
def alarm_wake(
    ctx: click.Context, leave: str | None, date_value: str | None, force: bool, clear: bool,
) -> None:
    """Set a single-use leave deadline, or clear it, and publish immediately."""
    from datetime import date

    from commutecompass.alarm_state import (
        ManualWake,
        clear_manual,
        parse_manual_deadline,
        save_manual,
    )
    from commutecompass.timeutil import now_nyc

    cfg = _load_config(ctx.obj["config_path"])
    if not cfg.alarm.window.enabled:
        click.echo("Error: alarm wake requires [alarm.window].enabled", err=True)
        sys.exit(EXIT_USAGE)
    if (clear and (leave is not None or date_value is not None or force)) or (not clear and not leave):
        click.echo("Error: use --leave TIME [--date YYYY-MM-DD] [--force] OR --clear", err=True)
        sys.exit(EXIT_USAGE)
    if clear:
        clear_manual(cfg)
    else:
        try:
            day = date.fromisoformat(date_value) if date_value is not None else None
            if day is not None and day.isoformat() != date_value:
                raise ValueError("date must be YYYY-MM-DD")
            assert leave is not None
            deadline = parse_manual_deadline(leave, day, now_nyc())
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(EXIT_USAGE)
        save_manual(cfg, ManualWake(deadline_at=deadline, force=force))
    # Persistence precedes HA: a failed push remains pending for the next publish.
    _alarm_publish_result(cfg)


@alarm.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def alarm_status(ctx: click.Context, as_json: bool) -> None:
    """Show HA's next alarm, master switch and daemon state/heartbeat."""
    import json as _json

    cfg = _load_config(ctx.obj["config_path"])
    base_url, token = _alarm_ha_or_exit(cfg)
    from commutecompass.alarm_daemon import HAUnavailable, fetch_states, parse_ha_datetime

    try:
        states = fetch_states(base_url, token)
    except HAUnavailable as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_TRANSIENT)
    a = cfg.alarm

    def st(entity: str) -> str | None:
        raw = states.get(entity, {}).get("state")
        return raw if isinstance(raw, str) else None

    nxt = parse_ha_datetime(st(a.next_alarm_entity))
    attrs = states.get(a.next_alarm_entity, {}).get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    hb = parse_ha_datetime(st(a.heartbeat_entity))
    from datetime import UTC, timedelta

    from commutecompass.alarm_state import load_manual
    from commutecompass.timeutil import now_nyc, to_nyc

    manual = load_manual(cfg, now_nyc(), dry_run=True) if a.window.enabled else None
    manual_info: dict[str, object] | None = None
    selected_manual = False
    if manual is not None:
        manual_wake = manual.deadline_at.astimezone(UTC) - timedelta(
            minutes=a.window.first_alarm_lead_minutes,
        )
        selected_manual = (
            st(a.enabled_entity) == "on" and nxt is not None
            and nxt.astimezone(UTC) == manual_wake and str(attrs.get("kind", "")) != "nap"
        )
        conflict = (
            nxt is not None and nxt.astimezone(UTC) < manual_wake
            and str(attrs.get("kind", "")) != "nap"
        )
        manual_info = {
            "deadline_at": manual.deadline_at.isoformat(),
            "wake_at": to_nyc(manual_wake).isoformat(),
            "force": manual.force, "selected": selected_manual, "conflict": conflict,
        }
    enabled_state = st(a.enabled_entity)
    enabled = True if enabled_state == "on" else False if enabled_state == "off" else None
    payload = {
        "manual": manual_info,
        "enabled": enabled,
        "next_alarm": nxt.isoformat() if nxt else None,
        "kind": attrs.get("kind"),
        "event": attrs.get("event"),
        "daemon_state": st(a.state_entity),
        "heartbeat": hb.isoformat() if hb else None,
    }
    if as_json:
        click.echo(_json.dumps(payload, indent=2))
        return
    enabled_label = "yes" if enabled is True else "no" if enabled is False else "unknown"
    click.echo(f"Alarm enabled: {enabled_label}")
    when = to_nyc(nxt).isoformat() if nxt else "none"
    kind = f" ({payload['kind']})" if payload["kind"] else ""
    if selected_manual and manual is not None:
        kind = f" (manual, leave {to_nyc(manual.deadline_at).strftime('%H:%M')})"
    effective = " (disabled)" if enabled is False else " (switch unknown)" if enabled is None else ""
    click.echo(f"Next alarm: {when}{kind}{effective}")
    if manual is not None and manual_info is not None:
        flags = (
            f"force={'yes' if manual.force else 'no'}, "
            f"conflict={'yes' if manual_info['conflict'] else 'no'}"
        )
        click.echo(f"Pending manual: leave {to_nyc(manual.deadline_at).isoformat()} ({flags})")
    if payload["event"]:
        click.echo(f"Event: {payload['event']}")
    click.echo(f"Daemon: {payload['daemon_state'] or 'unknown'} heartbeat={payload['heartbeat'] or 'never'}")


@alarm.command(name="nap")
@click.argument("minutes", type=click.IntRange(1, 24 * 60), required=False)
@click.option("--until", "until", help="Wake at this NYC time (HH:MM) instead of after MINUTES.")
@click.pass_context
def alarm_nap(ctx: click.Context, minutes: int | None, until: str | None) -> None:
    """Start a nap: ring after MINUTES (or at --until HH:MM). HA caps it at the next prep."""
    if (minutes is None) == (until is None):
        click.echo("Error: give exactly one of MINUTES or --until HH:MM", err=True)
        sys.exit(EXIT_USAGE)
    if until is not None:
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        from commutecompass.timeutil import NYC_TZ, now_nyc

        try:
            clock = _dt.strptime(until, "%H:%M").time()
        except ValueError:
            click.echo(f"Error: --until expects HH:MM, got {until!r}", err=True)
            sys.exit(EXIT_USAGE)
        now = now_nyc()
        target = _dt.combine(now.date(), clock, tzinfo=NYC_TZ)
        if target <= now:
            target = _dt.combine(now.date() + _td(days=1), clock, tzinfo=NYC_TZ)
        minutes = max(1, int((target.timestamp() - now.timestamp() + 59) // 60))
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_call(cfg, cfg.alarm.nap_script, {"minutes": minutes})
    click.echo(f"Nap set: {minutes} min (HA caps at the next prep time).")


@alarm.command(name="skip")
@click.pass_context
def alarm_skip(ctx: click.Context) -> None:
    """Skip the next alarm (auto-resets after it passes)."""
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_call(cfg, cfg.alarm.skip_script, {})
    # Explicit CLI skip/off retire pending manual intent only after HA accepts.
    from commutecompass.alarm_state import clear_manual

    clear_manual(cfg)
    click.echo("Next alarm skipped.")


@alarm.command(name="on")
@click.pass_context
def alarm_on(ctx: click.Context) -> None:
    """Turn the alarm master switch on."""
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_call(cfg, "input_boolean.turn_on", {"entity_id": cfg.alarm.enabled_entity})
    click.echo("Alarm enabled.")


@alarm.command(name="off")
@click.pass_context
def alarm_off(ctx: click.Context) -> None:
    """Turn the alarm master switch off."""
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_call(cfg, "input_boolean.turn_off", {"entity_id": cfg.alarm.enabled_entity})
    from commutecompass.alarm_state import clear_manual

    clear_manual(cfg)
    click.echo("Alarm disabled.")


@alarm.command(name="daemon")
@click.pass_context
def alarm_daemon_cmd(ctx: click.Context) -> None:
    """Run the long-lived ringer (polls HA, plays via PipeWire)."""
    cfg = _load_config(ctx.obj["config_path"])
    _alarm_ha_or_exit(cfg)
    from commutecompass.alarm_daemon import run_daemon

    run_daemon(cfg)

# ─────────── plan EVENT_ID ───────────────────────────────────────────────────


@cli.command()
@click.argument("selector")
@click.option(
    "--here",
    is_flag=True,
    help="Use the latest stored current location as origin (regardless of staleness).",
)
@click.option(
    "--from",
    "from_address",
    type=str,
    default=None,
    help="Geocode this address and use it as the origin for a what-if preview. "
    "Does NOT save the resulting plan (preview only).",
)
@click.pass_context
def plan(ctx: click.Context, selector: str, here: bool, from_address: str | None) -> None:
    """Replan a single event by selector (next / today:N / [id] / title).

    Without ``--here`` or ``--from``, saves the new plan into the store.
    With ``--from <address>``, runs a preview from a custom origin and does
    not modify stored plans — useful for "what if I were leaving from
    Brooklyn?" questions in chat.
    """
    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    from commutecompass.calendar_client import CalendarClient
    from commutecompass.geocode import geocode as _geocode
    from commutecompass.llm import OpencodeGoClient
    from commutecompass.models import Origin
    from commutecompass.planner import plan_event
    from commutecompass.store import Store
    from commutecompass.timeutil import logical_day_bounds_nyc, now_nyc
    from commutecompass.venues import VenueRegistry

    if here and from_address is not None:
        click.echo("Error: --here and --from are mutually exclusive.", err=True)
        sys.exit(EXIT_USAGE)

    store = Store(Path(cfg.paths.db_path))

    # Resolve the user-facing selector ("next" / "today:1" / "a1b2c3d4" / fuzzy
    # title) into a Google Calendar event id before reaching out to anything
    # heavier (calendar API, geocoder, routing).
    event_id = _resolve_or_exit(selector, store)

    token_path = Path(cfg.paths.oauth_token_path)
    cal_client = CalendarClient(
        client_secret_json=cfg.google_oauth_client_secret_json,
        token_path=token_path,
    )
    venues = VenueRegistry.load(Path(cfg.paths.venues_file))
    llm = OpencodeGoClient(
        endpoint=cfg.opencode_go.endpoint,
        token=cfg.opencode_go_token,
        model=cfg.opencode_go.model,
    )

    origin_override: Origin | None = None
    if here:
        cl = store.get_current_location(max_age_minutes=None)
        if cl is None:
            click.echo("No stored current location. Run `poll` first or check Home Assistant.", err=True)
            sys.exit(EXIT_UNRESOLVED)
        origin_override = Origin(
            address=f"{cl.lat:.6f},{cl.lon:.6f}",
            lat=cl.lat,
            lon=cl.lon,
        )
    elif from_address is not None:
        geo = _geocode(from_address, cfg.google_maps_api_key)
        if geo is None:
            click.echo(
                f"Error: could not geocode {from_address!r} (no result).", err=True
            )
            sys.exit(EXIT_UNRESOLVED)
        origin_override = Origin(
            address=geo.formatted_address or from_address,
            lat=geo.lat,
            lon=geo.lon,
        )

    is_preview = from_address is not None

    # Fetch the event from the store first (today's planned event)
    existing = store.get_plan(event_id)
    if existing is None:
        click.echo(f"No plan found for event {event_id}. Trying to fetch from calendar...")
        day_start, day_end = logical_day_bounds_nyc(now_nyc())
        events = cal_client.fetch_events(
            [CalendarSpec(id=cal.id, name=cal.name, enabled=cal.enabled) for cal in cfg.calendars],
            day_start,
            day_end,
        )
        matched_event = next((event for event in events if event.id == event_id), None)
        if matched_event is None:
            click.echo(f"Event {event_id} not found in today's calendars.")
            sys.exit(EXIT_NOT_FOUND)
        event = matched_event
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

    if not is_preview:
        store.upsert_plan(new_plan)

    if new_plan.error:
        click.echo(f"Plan error: {new_plan.error}")
    else:
        assert new_plan.prep_at is not None
        suffix = " (preview — not saved)" if is_preview else ""
        click.echo(f"Plan for {event_id}:{suffix}")
        if new_plan.leave_at is not None:
            click.echo(f"  Leave at:   {new_plan.leave_at.strftime('%H:%M:%S')}")
        else:
            click.echo("  Video call — no commute")
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
        sys.exit(EXIT_TRANSIENT)


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


# ─────────── status ──────────────────────────────────────────────────────────


def _route_travel_fields(route: Route | None) -> dict[str, float | str | None]:
    """Export the same persisted route timing used by the planner."""
    from commutecompass.routing import route_timing

    timing = route_timing(route)
    return {
        "travel_minutes": (
            timing.duration_seconds / 60.0 if timing.duration_seconds is not None else None
        ),
        "depart_at": timing.depart_at.isoformat() if timing.depart_at is not None else None,
        "arrive_at": timing.arrive_at.isoformat() if timing.arrive_at is not None else None,
    }


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human-readable text.")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Snapshot of today's plans, pings, location, and cache state.

    Designed as a diagnostic command for "why didn't I get my 8am ping today?"
    and as a skill the agent can call when the user asks operational
    questions.  Pure read — no API calls, no notifications.
    """
    import json as _json

    from commutecompass.store import Store
    from commutecompass.timeutil import now_nyc

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    store = Store(Path(cfg.paths.db_path))
    now = now_nyc()
    plans = store.today_plans()
    pings = store.all_pings_today()
    location = store.get_current_location(max_age_minutes=None)
    cache_stats = store.geocode_cache_stats()

    payload: dict[str, object] = {
        "now": now.isoformat(),
        "plans": [
            {
                "event_id": p.event.id,
                "title": p.event.title,
                "start": p.event.start.isoformat(),
                "leave_at": p.leave_at.isoformat() if p.leave_at else None,
                "prep_at": p.prep_at.isoformat() if p.prep_at else None,
                **_route_travel_fields(p.route),
                "leave_buffer_minutes": p.leave_buffer_minutes,
                "error": p.error,
                "resolved_source": (
                    p.event.location_resolved.source
                    if p.event.location_resolved
                    else None
                ),
            }
            for p in plans
        ],
        "pings": [
            {
                "id": p.id,
                "event_id": p.event_id,
                "kind": p.kind,
                "fire_at": p.fire_at.isoformat(),
                "fired": p.fired,
                "fired_at": p.fired_at.isoformat() if p.fired_at else None,
            }
            for p in pings
        ],
        "current_location": (
            {
                "lat": location.lat,
                "lon": location.lon,
                "zone": location.zone,
                "captured_at": location.captured_at.isoformat(),
                "age_seconds": int((now - location.captured_at).total_seconds()),
                "source": location.source,
                "accuracy_m": location.accuracy_m,
            }
            if location
            else None
        ),
        "geocode_cache": cache_stats,
    }

    if as_json:
        click.echo(_json.dumps(payload, indent=2, default=str))
        return

    # Human-readable text mode
    click.echo(f"now: {now.isoformat()}")
    click.echo(f"plans today: {len(plans)}")
    for plan in plans:
        marker = "✗" if plan.error else "·"
        leave = plan.leave_at.strftime("%I:%M %p").lstrip("0") if plan.leave_at else "—"
        prep = plan.prep_at.strftime("%I:%M %p").lstrip("0") if plan.prep_at else "—"
        err = f" error={plan.error}" if plan.error else ""
        click.echo(f"  {marker} {plan.event.id[:12]} {plan.event.title!r} prep={prep} leave={leave}{err}")
    click.echo(f"pings today: {len(pings)} ({sum(1 for x in pings if x.fired)} fired)")
    for ping in pings:
        state = "fired" if ping.fired else "pending"
        fa = ping.fire_at.strftime("%I:%M %p").lstrip("0")
        click.echo(f"  {state} {ping.kind} {ping.event_id[:12]} at {fa}")
    if location:
        age = int((now - location.captured_at).total_seconds())
        click.echo(
            f"location: lat={location.lat:.5f} lon={location.lon:.5f} "
            f"zone={location.zone or '-'} age={age}s source={location.source}"
        )
    else:
        click.echo("location: (none)")
    cnt = cache_stats["count"]
    oldest = cache_stats["oldest_cached_at"] or "—"
    newest = cache_stats["newest_cached_at"] or "—"
    click.echo(f"geocode_cache: {cnt} entries, oldest={oldest} newest={newest}")


# ─────────── geocode-cache ───────────────────────────────────────────────────


@cli.command(name="geocode-cache")
@click.option("--list", "as_list", is_flag=True, help="List all cached entries.")
@click.option("--invalidate", type=str, default=None, help="Delete the cache entry for this raw query string.")
@click.pass_context
def geocode_cache(ctx: click.Context, as_list: bool, invalidate: str | None) -> None:
    """Inspect or invalidate cached geocode lookups.

    The cache is keyed on the raw query string the resolver passed to Google
    Geocoding.  Use ``--list`` to see what's cached and ``--invalidate <raw>``
    to drop a stale entry (e.g. after a venue moves).
    """
    from commutecompass.store import Store

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    if invalidate is not None:
        removed = store.geocode_cache_invalidate(invalidate)
        if removed:
            click.echo(f"removed cache entry for {invalidate!r}")
        else:
            click.echo(f"no cache entry for {invalidate!r}")
            sys.exit(EXIT_NOT_FOUND)
        return

    if as_list:
        entries = store.geocode_cache_list()
        if not entries:
            click.echo("geocode cache is empty")
            return
        for e in entries:
            click.echo(f"{e['cached_at']}  {e['raw']}")
        return

    # Default: just print summary.
    stats = store.geocode_cache_stats()
    click.echo(
        f"{stats['count']} entries (oldest={stats['oldest_cached_at']}, "
        f"newest={stats['newest_cached_at']})"
    )


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
@click.argument("selector")
@click.option(
    "--add-prep",
    type=int,
    required=True,
    help="Minutes to add to the prep buffer (negative shrinks it). "
    "Shifts prep_at earlier by this many minutes.",
)
@click.option(
    "--idempotency-key",
    type=str,
    default=None,
    help="Opaque key.  If supplied and previously seen, this invocation is a "
    "no-op (exit 0).  Use a stable upstream correlation id so a retried "
    "skill invocation does not stack adjustments.",
)
@click.pass_context
def adjust(
    ctx: click.Context,
    selector: str,
    add_prep: int,
    idempotency_key: str | None,
) -> None:
    """Shift today's prep time for SELECTOR by --add-prep minutes.

    SELECTOR accepts ``next``, ``today:N``, an 8-char id prefix, a full
    event_id, or a fuzzy title fragment.

    Use case: "I need to shower before this event, add 45 min" →
    ``adjust next --add-prep 45``.  The existing poll cycle re-fires the
    rescheduled prep ping at its new time.
    """
    from commutecompass.store import Store

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    store = Store(Path(cfg.paths.db_path))
    event_id = _resolve_or_exit(selector, store)
    _apply_adjust(
        store, event_id, add_prep=add_prep, idempotency_key=idempotency_key
    )


def _apply_adjust(
    store: Store,
    event_id: str,
    *,
    add_prep: int,
    idempotency_key: str | None = None,
) -> None:
    """Core prep-shift logic shared by ``adjust`` and (indirectly) ``undo``.

    ``undo`` doesn't reuse this directly — it restores the exact prior
    ``prep_at`` from ``adjust_log.prev_prep_at`` rather than re-applying an
    inverse offset — but the structure is kept aligned so future variants
    can fall through here.
    """
    from datetime import timedelta

    from commutecompass.format import format_prep_ping
    from commutecompass.models import PingEntry
    from commutecompass.timeutil import now_nyc

    plan = store.get_plan(event_id)
    if plan is None:
        click.echo(f"No plan found for event {event_id}.", err=True)
        sys.exit(EXIT_NOT_FOUND)
    if plan.prep_at is None or (plan.leave_at is None and not plan.event.virtual):
        click.echo(
            f"Event {event_id} has no scheduled prep/leave time — cannot adjust.",
            err=True,
        )
        sys.exit(EXIT_UNRESOLVED)

    # Log the row BEFORE mutating so the prev_prep_at captures the pre-shift
    # value.  When an idempotency key is supplied, a duplicate insert returns
    # None and we no-op (matches the previous behaviour of record_adjust_key).
    prev_prep_at = plan.prep_at
    actual_key = store.record_adjust(
        event_id,
        add_prep_minutes=add_prep,
        prev_prep_at=prev_prep_at,
        key=idempotency_key,
    )
    if actual_key is None:
        click.echo(
            f"adjust {event_id}: idempotency key already applied — no-op."
        )
        return

    now = now_nyc()
    new_prep_at = plan.prep_at - timedelta(minutes=add_prep)
    new_prep_at = max(new_prep_at, now)
    plan.prep_at = new_prep_at

    store.upsert_plan(plan)

    if new_prep_at > now:
        import uuid

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
    leave_note = (
        f"leave_at {plan.leave_at.strftime('%I:%M %p').lstrip('0')} unchanged."
        if plan.leave_at is not None
        else "video call, no leave time."
    )
    click.echo(
        f"Adjusted prep for {event_id}: prep_at {new_prep_at.strftime('%I:%M %p').lstrip('0')} "
        f"({abs(add_prep)} min {direction}); {leave_note}"
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
    from commutecompass.config import (
        EXTERNALLY_SCHEDULED_KEYS,
        ConfigSetError,
        ConfigSetValidationError,
        update_config_field,
    )

    config_path: Path = ctx.obj["config_path"]
    try:
        coerced = update_config_field(config_path, key, value)
    except ConfigSetValidationError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    except ConfigSetError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_USAGE)
    except OSError as exc:
        click.echo(f"Error writing {config_path}: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    click.echo(f"{key} = {coerced!r}")
    if key in EXTERNALLY_SCHEDULED_KEYS:
        click.echo(
            f"Note: {key} is not read at runtime — the schedule is set by your "
            "systemd timer or cron entry. Update that to change when the job runs.",
            err=True,
        )


@config.command(name="unset")
@click.argument("key")
@click.pass_context
def config_unset(ctx: click.Context, key: str) -> None:
    """Remove an allowlisted KEY from config.toml so the schema default applies.

    Lets chat clear settings cleanly — e.g. unsetting
    ``scheduling.quiet_hours_start`` and ``...end`` turns off quiet hours
    without the "set a 1-minute window" hack.
    """
    from commutecompass.config import ConfigSetError, delete_config_field

    config_path: Path = ctx.obj["config_path"]
    try:
        removed = delete_config_field(config_path, key)
    except ConfigSetError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_USAGE)
    except OSError as exc:
        click.echo(f"Error writing {config_path}: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    if removed:
        click.echo(f"unset {key} (now using schema default)")
    else:
        click.echo(f"{key} was already unset (schema default in effect)")


@config.command(name="reset")
@click.option(
    "--yes",
    is_flag=True,
    help="Actually remove every allowlisted override. Without this, prints a preview only.",
)
@click.pass_context
def config_reset(ctx: click.Context, yes: bool) -> None:
    """Drop every allowlisted override so chat-tweakable fields revert to defaults.

    Without ``--yes``, prints the would-remove list and exits ``EXIT_USAGE``.
    The non-allowlisted blocks (``[origin]``, ``[paths]``, ``[[calendars]]``,
    MTA URLs, secrets) are NEVER touched.
    """
    from commutecompass.config import (
        ConfigSetError,
        delete_config_field,
        list_overridden_allowlist_keys,
    )

    config_path: Path = ctx.obj["config_path"]
    try:
        present = list_overridden_allowlist_keys(config_path)
    except OSError as exc:
        click.echo(f"Error reading {config_path}: {exc}", err=True)
        sys.exit(EXIT_CONFIG)

    if not present:
        click.echo("No allowlisted overrides set — nothing to reset.")
        return

    if not yes:
        click.echo("Would remove the following overrides (re-run with --yes):")
        for k in present:
            click.echo(f"  {k}")
        sys.exit(EXIT_USAGE)

    removed = 0
    for k in present:
        try:
            if delete_config_field(config_path, k):
                removed += 1
        except (ConfigSetError, OSError) as exc:
            click.echo(f"Error removing {k}: {exc}", err=True)
            sys.exit(EXIT_CONFIG)
    click.echo(f"Removed {removed} override(s); defaults now in effect.")


# ─────────── snooze / mute / unmute / undo / mta-alerts ──────────────────────


@cli.command()
@click.argument("selector")
@click.option(
    "--minutes",
    type=int,
    default=None,
    help="Shift the pending prep ping forward by N minutes (negative pulls it earlier).",
)
@click.option(
    "--skip",
    is_flag=True,
    help="Mark the pending prep ping fired without sending it.",
)
@click.pass_context
def snooze(
    ctx: click.Context, selector: str, minutes: int | None, skip: bool
) -> None:
    """Snooze or skip the unfired *prep* ping for SELECTOR.

    Only operates on prep pings: leave pings are operationally critical and
    intentionally not snoozable via chat.  Exactly one of ``--minutes`` or
    ``--skip`` must be supplied.
    """
    from commutecompass.store import Store
    from commutecompass.timeutil import now_nyc

    if (minutes is None) == (not skip):
        click.echo(
            "Error: pass exactly one of --minutes N or --skip.", err=True
        )
        sys.exit(EXIT_USAGE)

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    event_id = _resolve_or_exit(selector, store)
    ping = store.get_pending_ping(event_id, kind="prep")
    if ping is None:
        click.echo(
            f"No pending prep ping for {event_id} (already fired, cancelled, or never scheduled).",
            err=True,
        )
        sys.exit(EXIT_NOT_FOUND)

    if skip:
        store.mark_fired(ping.id, now_nyc())
        click.echo(f"Skipped prep ping for {event_id} ({ping.id[:8]}).")
        return

    import uuid
    from datetime import timedelta

    from commutecompass.models import PingEntry

    new_fire = ping.fire_at + timedelta(minutes=minutes or 0)
    # Snoozing supersedes the old generation without reusing its identity or retries.
    store.schedule_ping(
        PingEntry(
            id=str(uuid.uuid4()),
            event_id=ping.event_id,
            kind=ping.kind,
            fire_at=new_fire,
            message=ping.message,
        )
    )
    direction = "later" if (minutes or 0) >= 0 else "earlier"
    click.echo(
        f"Snoozed prep for {event_id} by {abs(minutes or 0)} min {direction} → "
        f"{new_fire.strftime('%I:%M %p').lstrip('0')}."
    )


@cli.command()
@click.argument("selector", required=False)
@click.option(
    "--today",
    "today",
    is_flag=True,
    help="Mute every event in today's plans until end-of-day.",
)
@click.pass_context
def mute(ctx: click.Context, selector: str | None, today: bool) -> None:
    """Suppress notifications for an event (forever) or all of today's events.

    Enforced inside the poll loop right before notifying on a claimed ping —
    mid-day replans that schedule fresh pings are covered automatically.
    A ping that has already fired stays fired; muting is forward-looking.
    """
    from commutecompass.store import Store
    from commutecompass.timeutil import logical_day_bounds_nyc

    if today and selector:
        click.echo("Error: --today and a selector are mutually exclusive.", err=True)
        sys.exit(EXIT_USAGE)
    if not today and not selector:
        click.echo(
            "Error: pass a selector or --today. See `commutecompass mute --help`.",
            err=True,
        )
        sys.exit(EXIT_USAGE)

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    if today:
        plans = store.today_plans()
        if not plans:
            click.echo("No plans today to mute.")
            return
        _day_start, day_end = logical_day_bounds_nyc()
        muted = 0
        for plan in plans:
            store.mute_event(plan.event.id, expires_at=day_end)
            store.cancel_pings(plan.event.id)
            muted += 1
        click.echo(
            f"Muted {muted} event(s) for today; pings cancelled. "
            f"Mutes expire at {day_end.strftime('%I:%M %p').lstrip('0')}."
        )
        return

    assert selector is not None
    event_id = _resolve_or_exit(selector, store)
    store.mute_event(event_id)
    cancelled = store.cancel_pings(event_id)
    click.echo(
        f"Muted event {event_id} (forever). Cancelled {cancelled} pending ping(s)."
    )


@cli.command()
@click.argument("selector")
@click.pass_context
def unmute(ctx: click.Context, selector: str) -> None:
    """Lift a mute on SELECTOR. Already-cancelled pings stay cancelled —
    a fresh ``poll`` or location-driven replan will schedule new ones."""
    from commutecompass.store import Store

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    event_id = _resolve_or_exit(selector, store)
    removed = store.unmute_event(event_id)
    if removed:
        click.echo(f"Unmuted event {event_id}.")
    else:
        click.echo(f"Event {event_id} was not muted.")


@cli.command()
@click.argument("selector", required=False)
@click.pass_context
def undo(ctx: click.Context, selector: str | None) -> None:
    """Revert the most recent adjust (globally or scoped to SELECTOR).

    Restores ``prep_at`` to the exact value recorded in ``adjust_log``, marks
    the row ``undone=1``, and reschedules the prep ping accordingly.  Running
    ``undo`` again walks one more step back through the adjust history.
    """
    import uuid

    from commutecompass.format import format_prep_ping
    from commutecompass.models import PingEntry
    from commutecompass.store import Store
    from commutecompass.timeutil import now_nyc

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    event_id_filter: str | None = None
    if selector is not None:
        event_id_filter = _resolve_or_exit(selector, store)

    row = store.last_adjust(event_id_filter)
    if row is None:
        scope = f"for {event_id_filter}" if event_id_filter else ""
        click.echo(f"No adjust to undo{(' ' + scope) if scope else ''}.", err=True)
        sys.exit(EXIT_NOT_FOUND)

    plan = store.get_plan(row.event_id)
    if plan is None:
        # Plan rolled off — still mark the row undone so the next undo can
        # progress, but tell the user nothing was restored.
        store.mark_adjust_undone(row.key)
        click.echo(
            f"adjust_log row for {row.event_id} cleared, but the plan is no longer in today's set."
        )
        return

    assert row.prev_prep_at is not None
    now = now_nyc()
    restored = row.prev_prep_at
    restored = max(restored, now)
    plan.prep_at = restored
    store.upsert_plan(plan)
    store.mark_adjust_undone(row.key)

    if restored > now:
        store.schedule_ping(
            PingEntry(
                id=str(uuid.uuid4()),
                event_id=row.event_id,
                kind="prep",
                fire_at=restored,
                fired=False,
                message=format_prep_ping(plan),
            )
        )

    click.echo(
        f"Reverted adjust on {row.event_id} ({row.add_prep_minutes:+d} min). "
        f"prep_at now {restored.strftime('%I:%M %p').lstrip('0')}."
    )


@cli.command(name="gtfs-refresh")
@click.pass_context
def gtfs_refresh(ctx: click.Context) -> None:
    """Download the static subway GTFS and rebuild the identity-bridge index.

    Tries the supplemented feed first and falls back to the regular subway
    feed.  The index (``paths.gtfs_db_path``) is swapped atomically; on any
    failure the previous index is kept.  Exits 75 when neither feed could be
    fetched/imported, 78 when the vendored allowlist does not match the feed.
    """
    from commutecompass.gtfs_allowlist import AllowlistError, load_allowlist
    from commutecompass.gtfs_index import GtfsRefreshError, refresh

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    # Recheck mutable config/filesystem aliases before even acquiring the lock
    # (which can create state directories), downloading, or building the index.
    try:
        cfg.paths.ensure_distinct_db_paths()
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    gtfs_db = Path(cfg.paths.gtfs_db_path)

    try:
        allowlist = load_allowlist()
    except AllowlistError as exc:
        click.echo(f"Error: invalid GTFS bridge allowlist: {exc}", err=True)
        sys.exit(EXIT_CONFIG)

    def _run() -> None:
        try:
            stats = refresh(gtfs_db, allowlist=allowlist)
        except GtfsRefreshError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(EXIT_CONFIG if exc.config_error else EXIT_TRANSIENT)
        click.echo(
            f"GTFS index refreshed from {stats.feed_kind} feed {stats.feed_version} "
            f"(covers {stats.coverage_start.isoformat()}..{stats.coverage_end.isoformat()}): "
            f"{stats.trips} trips, {stats.stop_times} stop times, "
            f"{stats.rejected_trips} rejected → {gtfs_db}"
        )

    _run_with_job_lock(cfg, "gtfs-refresh", _run)


@cli.command(name="mta-alerts")
@click.pass_context
def mta_alerts(ctx: click.Context) -> None:
    """Show MTA alerts that touch any leg of today's planned routes.

    Reuses the digest's alert-block renderer so the chat surface matches the
    morning digest format.  Fetches fresh — no in-process cache survives
    between CLI calls (acceptable cost: three HTTP requests).
    """
    from commutecompass.format import format_alerts_block
    from commutecompass.mta import alerts_affecting_route, fetch_alerts
    from commutecompass.store import Store

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)
    store = Store(Path(cfg.paths.db_path))

    plans = store.today_plans()
    plans_with_routes = [p for p in plans if p.route is not None and p.leave_at is not None]
    if not plans_with_routes:
        click.echo("No planned routes today — nothing to filter alerts against.")
        return

    try:
        alerts = fetch_alerts(
            subway_url=cfg.mta.subway_alerts_url,
            lirr_url=cfg.mta.lirr_alerts_url,
            bus_url=cfg.mta.bus_alerts_url,
        )
    except Exception as exc:  # noqa: BLE001 - any fetch failure maps to EXIT_TRANSIENT
        click.echo(f"Error: failed to fetch MTA alerts: {exc}", err=True)
        sys.exit(EXIT_TRANSIENT)

    seen_ids: set[str] = set()
    matched = []
    for plan in plans_with_routes:
        assert plan.route is not None and plan.leave_at is not None
        for alert in alerts_affecting_route(alerts, plan.route, plan.leave_at):
            if alert.id in seen_ids:
                continue
            seen_ids.add(alert.id)
            matched.append(alert)

    if not matched:
        click.echo("No active alerts affect today's commute.")
        return

    click.echo(format_alerts_block(matched))


@cli.command(name="realtime")
@click.pass_context
def realtime_cmd(ctx: click.Context) -> None:
    """Show real-time departure delays for today's planned boarding legs.

    Reads the MTA GTFS-RT trip-update feeds and reports how late (if at all) the
    line each plan boards is running.  Honors ``realtime.enabled`` — when
    disabled it says so rather than fetching.
    """
    from commutecompass.realtime import realtime_delay
    from commutecompass.store import Store

    config_path: Path = ctx.obj["config_path"]
    cfg = _load_config(config_path)

    if not cfg.realtime.enabled:
        click.echo("Real-time departures are disabled (set realtime.enabled = true).")
        return

    store = Store(Path(cfg.paths.db_path))
    plans = store.today_plans()
    plans_with_routes = [p for p in plans if p.route is not None and p.leave_at is not None]
    if not plans_with_routes:
        click.echo("No planned routes today — nothing to check.")
        return

    any_failure = False
    for plan in plans_with_routes:
        assert plan.route is not None
        delay = realtime_delay(plan.route, plan.event.start, cfg.realtime)
        boarding = next((leg for leg in plan.route.legs if leg.mode == "TRANSIT"), None)
        if boarding and boarding.line and boarding.departure_stop:
            board_desc = f"{boarding.line} from {boarding.departure_stop}"
        else:
            board_desc = "(no transit leg)"
        description = delay.reason or delay.detail or delay.status.replace("_", " ")
        click.echo(f"{plan.event.title}: {board_desc} → {delay.status}: {description}")
        if delay.departures:
            times = ", ".join(d.strftime("%I:%M %p").lstrip("0") for d in delay.departures)
            click.echo(f"  Feed departures (not a measured delay): {times}")
        if delay.feed_failed:
            any_failure = True
            click.echo("  One or more realtime feeds failed; results may be partial.")
    if any_failure:
        sys.exit(EXIT_TRANSIENT)


# ─────────── bot (stub) ──────────────────────────────────────────────────────


@cli.command()
def bot() -> None:
    """Telegram bot mode (stub)."""
    click.echo("Telegram bot mode is not yet implemented.")


# ─────────── entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point registered as the `commutecompass` console script."""
    cli()
