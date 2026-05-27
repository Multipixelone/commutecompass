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


def _load_config(config_path: Path) -> "Config":
    """Load and return the Config, exiting gracefully on error."""
    # Import module to allow patching at the right spot
    from commutecompass import config as config_mod

    try:
        return config_mod.load_config(config_path)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(EXIT_CONFIG)


def _resolve_or_exit(selector: str, store: "Store") -> str:
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
def plan(ctx: click.Context, selector: str, here: bool, from_address: Optional[str]) -> None:
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
    from commutecompass.venues import VenueRegistry
    from commutecompass.timeutil import now_nyc

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

    origin_override: Optional[Origin] = None
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
        now = now_nyc()
        events = cal_client.fetch_events(
            [CalendarSpec(id=cal.id, name=cal.name, enabled=cal.enabled) for cal in cfg.calendars],
            now,
            now,
        )
        if not events:
            click.echo(f"Event {event_id} not found in today's calendars.")
            sys.exit(EXIT_NOT_FOUND)
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

    if not is_preview:
        store.upsert_plan(new_plan)

    if new_plan.error:
        click.echo(f"Plan error: {new_plan.error}")
    else:
        assert new_plan.leave_at is not None
        assert new_plan.prep_at is not None
        suffix = " (preview — not saved)" if is_preview else ""
        click.echo(f"Plan for {event_id}:{suffix}")
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
def geocode_cache(ctx: click.Context, as_list: bool, invalidate: Optional[str]) -> None:
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
    idempotency_key: Optional[str],
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
    store: "Store",
    event_id: str,
    *,
    add_prep: int,
    idempotency_key: Optional[str] = None,
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
    if plan.prep_at is None or plan.leave_at is None:
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
    if new_prep_at < now:
        new_prep_at = now
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
    click.echo(
        f"Adjusted prep for {event_id}: prep_at {new_prep_at.strftime('%I:%M %p').lstrip('0')} "
        f"({abs(add_prep)} min {direction}); leave_at "
        f"{plan.leave_at.strftime('%I:%M %p').lstrip('0')} unchanged."
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
        sys.exit(EXIT_USAGE)
    except OSError as exc:
        click.echo(f"Error writing {config_path}: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    click.echo(f"{key} = {coerced!r}")


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
    ctx: click.Context, selector: str, minutes: Optional[int], skip: bool
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

    from datetime import timedelta

    new_fire = ping.fire_at + timedelta(minutes=minutes or 0)
    ping.fire_at = new_fire
    store.schedule_ping(ping)
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
def mute(ctx: click.Context, selector: Optional[str], today: bool) -> None:
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
def undo(ctx: click.Context, selector: Optional[str]) -> None:
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

    event_id_filter: Optional[str] = None
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
    if restored < now:
        restored = now
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
    except Exception as exc:
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


# ─────────── bot (stub) ──────────────────────────────────────────────────────


@cli.command()
def bot() -> None:
    """Telegram bot mode (stub)."""
    click.echo("Telegram bot mode is not yet implemented.")


# ─────────── entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point registered as the `commutecompass` console script."""
    cli()
