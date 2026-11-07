"""SQLite persistence layer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from commutecompass.models import (
    AdjustRow,
    CurrentLocation,
    PingEntry,
    Plan,
    ResolvedLocation,
    Route,
)


def _alarm_instant(value: datetime) -> datetime:
    """Require aware alarm timestamps and normalize their absolute identity."""
    if value.utcoffset() is None:
        raise ValueError("alarm timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _logical_day_window(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """Exclusive NYC wall-day bounds, without subtracting across a DST fold."""
    from commutecompass.timeutil import logical_day_bounds_nyc

    start, _ = logical_day_bounds_nyc(reference)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _now_iso() -> str:
    """Return the current time as an ISO-8601 string in NYC tz.

    Centralised here so write paths never accidentally drop the tz offset.
    """
    # Local import to avoid a cycle at module load.
    from commutecompass.timeutil import now_nyc

    return now_nyc().isoformat()


def _json_dumps(obj: object) -> str:
    """Serialize an object to JSON, handling datetime offset format."""
    return json.dumps(obj, default=_json_serializer)


def _utc_instants(obj: object) -> object:
    """Recursively replace aware datetimes with their UTC instant."""
    if isinstance(obj, datetime):
        return obj.astimezone(UTC) if obj.utcoffset() is not None else obj
    if isinstance(obj, dict):
        return {key: _utc_instants(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_utc_instants(value) for value in obj]
    return obj


def _same_plan(a: Plan, b: Plan) -> bool:
    """Compare plans field-by-field with datetimes compared as UTC instants.

    Pydantic equality compares datetimes with ``==``.  Per PEP 495, an
    inter-zone comparison involving an ambiguous local time (the NYC fall-back
    hour) is always unequal, so a ``ZoneInfo`` plan never equals its own
    fixed-offset JSON round-trip during that hour.  Same-zone comparisons have
    the opposite flaw: they ignore ``fold`` and treat 01:30 EDT and 01:30 EST
    as equal.  Normalising every datetime to UTC first avoids both problems;
    the same instant written with different offsets still compares equal.
    """
    return _utc_instants(a.model_dump()) == _utc_instants(b.model_dump())


def _json_loads(raw: str) -> dict[str, Any]:
    """Parse a JSON string."""
    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return cast(dict[str, Any], loaded)
    return {}


def _json_serializer(obj: object) -> str:
    """JSON serializer for objects not serializable by default."""
    if isinstance(obj, datetime):
        # ISO-8601 with offset — preserved by parsing back to datetime
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Bump when the on-disk schema changes.  Stored in SQLite's built-in
# ``PRAGMA user_version`` so future migrations can branch on the version the DB
# was last initialised at, instead of probing every table with PRAGMA
# table_info.  The existing column adds remain idempotent and run unconditionally.
SCHEMA_VERSION = 2

# Alert-specific announcement retries, independent of the route's saved delta.
ALERT_NOTIFICATION_MAX_ATTEMPTS = 5
ALERT_NOTIFICATION_GRACE_SECONDS = 15 * 60
ALERT_NOTIFICATION_LEASE_SECONDS = 60


@dataclass(frozen=True)
class PollReplanOutcome:
    """Notification disposition of the final rows in a committed replan."""

    actionable_due: bool
    imminent_leave_consumed: bool


class Store:
    """SQLite store for plans, pings, geocode cache, and alert ledger."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with WAL + busy_timeout enabled.

        ``journal_mode=WAL`` lets readers and writers proceed concurrently,
        which is the right mode for the morning/poll job overlap.  The busy
        timeout gives competing writers a chance to acquire the lock instead
        of immediately raising ``OperationalError``.  ``synchronous=NORMAL``
        is safe under WAL and meaningfully faster than ``FULL``.
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create all tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS plans (
                    event_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    planned_at TEXT NOT NULL,
                    event_start TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pings (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fire_at TEXT NOT NULL,
                    fired INTEGER NOT NULL DEFAULT 0,
                    fired_at TEXT,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pings_pending ON pings(fired, fire_at);
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    raw TEXT PRIMARY KEY,
                    resolved_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS route_cache (
                    origin_key TEXT NOT NULL,
                    dest_value TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    route_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    PRIMARY KEY (origin_key, dest_value, mode)
                );
                CREATE TABLE IF NOT EXISTS alerts_seen (
                    alert_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (alert_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS current_location (
                    id TEXT PRIMARY KEY DEFAULT 'singleton',
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    zone TEXT,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adjust_log (
                    key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_mutes (
                    event_id TEXT PRIMARY KEY,
                    muted_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS alarm_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    manual_deadline TEXT,
                    manual_force INTEGER NOT NULL DEFAULT 0,
                    manual_version INTEGER NOT NULL DEFAULT 0,
                    bound_wake TEXT,
                    bound_deadline TEXT,
                    bound_version INTEGER,
                    last_publish_json TEXT
                );

                CREATE TABLE IF NOT EXISTS job_heartbeat (
                    job TEXT PRIMARY KEY,
                    last_success TEXT NOT NULL
                );
            """)
            # Migrate adjust_log: add columns required for `undo` (single-step
            # restoration of the exact previous prep_at + undone flag).
            adj_cols = {row[1] for row in conn.execute("PRAGMA table_info(adjust_log)").fetchall()}
            if "add_prep_minutes" not in adj_cols:
                conn.execute("ALTER TABLE adjust_log ADD COLUMN add_prep_minutes INTEGER")
            if "prev_prep_at" not in adj_cols:
                conn.execute("ALTER TABLE adjust_log ADD COLUMN prev_prep_at TEXT")
            if "undone" not in adj_cols:
                conn.execute(
                    "ALTER TABLE adjust_log ADD COLUMN undone INTEGER NOT NULL DEFAULT 0"
                )
            # The schema transaction also serializes additive migration/dedupe.
            ping_cols = {row[1] for row in conn.execute("PRAGMA table_info(pings)").fetchall()}
            if "retired" not in ping_cols:
                conn.execute("ALTER TABLE pings ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
            # Preserve history; only proven duplicate pending generations retire.
            index_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("idx_ping_event_kind_unfired",),
            ).fetchone()
            if not index_exists:
                conn.execute("""
                    UPDATE pings SET fired = 1, retired = 1 WHERE fired = 0 AND rowid IN (
                        SELECT rowid FROM (
                            SELECT rowid, ROW_NUMBER() OVER (
                                PARTITION BY event_id, kind
                                ORDER BY julianday(fire_at) DESC, rowid DESC
                            ) AS rank
                            FROM pings WHERE fired = 0
                        ) WHERE rank > 1
                    )
                """)
                conn.execute("""
                    CREATE UNIQUE INDEX idx_ping_event_kind_unfired
                    ON pings(event_id, kind) WHERE fired = 0
                """)
            # Phase 1: add gps_accuracy column to current_location if missing.
            cl_cols = {row[1] for row in conn.execute("PRAGMA table_info(current_location)").fetchall()}
            if "accuracy_m" not in cl_cols:
                conn.execute("ALTER TABLE current_location ADD COLUMN accuracy_m REAL")
            # Add send_attempts to pings: counts failed sends so the poll loop
            # can bound cross-tick re-fire of actionable pings (see release_ping).
            ping_cols = {row[1] for row in conn.execute("PRAGMA table_info(pings)").fetchall()}
            if "send_attempts" not in ping_cols:
                conn.execute(
                    "ALTER TABLE pings ADD COLUMN send_attempts INTEGER NOT NULL DEFAULT 0"
                )
            # Existing rows remain current until explicitly superseded.
            if "retired" not in ping_cols:
                conn.execute("ALTER TABLE pings ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_notifications (
                    alert_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    day_start TEXT NOT NULL,
                    message TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claimed_until TEXT,
                    delivered_at TEXT,
                    PRIMARY KEY (alert_id, event_id, day_start)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_processing (
                    alert_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    claim_token TEXT,
                    claimed_until TEXT,
                    PRIMARY KEY (alert_id, event_id)
                )
            """)
            # Stamp the schema version last, once all tables/columns exist.
            # PRAGMA can't be parameterised, but SCHEMA_VERSION is a trusted int.
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    def schema_version(self) -> int:
        """Return the schema version stamped in the database header."""
        with self._connect() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    # ── Alarm state (additive; independent of the plan schema version) ──────────

    def _read_alarm_state(self) -> sqlite3.Row | None:
        """Read without creating a database, journal, or missing state table."""
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alarm_state'"
            ).fetchone()
            return conn.execute("SELECT * FROM alarm_state WHERE id = 1").fetchone() if exists else None
        finally:
            conn.close()

    def load_alarm_manual(
        self, now: datetime, *, dry_run: bool = False
    ) -> tuple[datetime, bool, int] | None:
        """Read deadline, force, and identity; retire expiry unless dry-run."""
        now_utc = _alarm_instant(now)
        if dry_run:
            row = self._read_alarm_state()
        else:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM alarm_state WHERE id = 1").fetchone()
                if (
                    row is not None
                    and row["manual_deadline"] is not None
                    and datetime.fromisoformat(row["manual_deadline"]) <= now_utc
                ):
                    conn.execute("""
                        UPDATE alarm_state SET manual_deadline = NULL, manual_force = 0,
                            bound_wake = NULL, bound_deadline = NULL, bound_version = NULL
                        WHERE id = 1
                    """)
                    return None
        if row is None or row["manual_deadline"] is None:
            return None
        deadline = datetime.fromisoformat(row["manual_deadline"])
        return (
            (deadline, bool(row["manual_force"]), int(row["manual_version"]))
            if deadline > now_utc else None
        )

    def save_alarm_manual(self, deadline_at: datetime, force: bool = False) -> None:
        """Replace the manual intent and invalidate every previous binding."""
        deadline = _alarm_instant(deadline_at).isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO alarm_state (id, manual_deadline, manual_force, manual_version)
                VALUES (1, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    manual_deadline = excluded.manual_deadline,
                    manual_force = excluded.manual_force,
                    manual_version = alarm_state.manual_version + 1,
                    bound_wake = NULL, bound_deadline = NULL, bound_version = NULL
            """, (deadline, int(force)))

    def clear_alarm_manual(self) -> None:
        """Clear intent and binding, preserving the last successful publication."""
        with self._connect() as conn:
            conn.execute("""
                UPDATE alarm_state SET manual_deadline = NULL, manual_force = 0,
                    bound_wake = NULL, bound_deadline = NULL, bound_version = NULL
                WHERE id = 1
            """)

    def bind_alarm_manual(
        self,
        wake_at: datetime,
        deadline_at: datetime,
        *,
        expected_deadline_at: datetime | None = None,
        expected_version: int | None = None,
    ) -> None:
        """Compare intent identity then bind; calendar deadlines may cross 02:00.

        A complete expected identity proves which manual intent the caller selected;
        its calendar deadline need not share the manual deadline's logical day.
        Without an identity, compatibility calls require an exact deadline match.
        """
        wake = _alarm_instant(wake_at)
        deadline = _alarm_instant(deadline_at)
        if (expected_deadline_at is None) != (expected_version is None):
            return
        expected = _alarm_instant(expected_deadline_at) if expected_deadline_at else deadline
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT manual_deadline, manual_version FROM alarm_state WHERE id = 1"
            ).fetchone()
            if row is None or row[0] is None:
                return
            manual_deadline = datetime.fromisoformat(row[0])
            if manual_deadline != expected:
                return
            if expected_version is not None and row[1] != expected_version:
                return
            conn.execute("""
                UPDATE alarm_state SET bound_wake = ?, bound_deadline = ?,
                    bound_version = manual_version
                WHERE id = 1
            """, (wake.isoformat(), deadline.isoformat()))

    def clear_alarm_manual_for_wake(self, wake_at: datetime) -> bool:
        """Compare-and-clear a completed binding; a replaced intent is untouched."""
        wake = _alarm_instant(wake_at).isoformat()
        with self._connect() as conn:
            result = conn.execute("""
                UPDATE alarm_state SET manual_deadline = NULL, manual_force = 0,
                    bound_wake = NULL, bound_deadline = NULL, bound_version = NULL
                WHERE id = 1 AND bound_wake = ? AND manual_deadline IS NOT NULL
                    AND manual_version = bound_version
            """, (wake,))
            return result.rowcount == 1

    def load_alarm_last_publish(self) -> dict[str, object] | None:
        """Return the last successful result without creating or changing state."""
        row = self._read_alarm_state()
        if row is None or row["last_publish_json"] is None:
            return None
        payload: object = json.loads(row["last_publish_json"])
        if not isinstance(payload, dict):
            raise TypeError("stored alarm publication must be a JSON object")
        return cast(dict[str, object], payload)

    def save_alarm_last_publish(self, payload: dict[str, object]) -> None:
        """Persist a caller-serialized successful result without changing intent."""
        encoded = json.dumps(payload, allow_nan=False)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO alarm_state (id, last_publish_json) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET last_publish_json = excluded.last_publish_json
            """, (encoded,))

    # ── Plan CRUD ──────────────────────────────────────────────────────────────

    def upsert_plan(self, plan: Plan) -> None:
        """Insert or replace a plan."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (event_id, plan_json, planned_at, event_start)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    plan_json = excluded.plan_json,
                    planned_at = excluded.planned_at,
                    event_start = excluded.event_start
                """,
                (
                    plan.event.id,
                    _json_dumps(plan.model_dump()),
                    _now_iso(),
                    plan.event.start.isoformat(),
                ),
            )

    def get_plan(self, event_id: str) -> Plan | None:
        """Retrieve a plan by event ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan_json FROM plans WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        data = _json_loads(row[0])
        return Plan.model_validate(data)

    def today_plans(self) -> list[Plan]:
        """Return plans for the current logical day in America/New_York.

        Logical day boundary is 02:00-01:59 local time.
        """
        today_start, today_end = _logical_day_window()
        with self._connect() as conn:
            rows = conn.execute("SELECT plan_json FROM plans").fetchall()
        plans = [Plan.model_validate_json(row[0]) for row in rows]
        return sorted(
            (plan for plan in plans if today_start <= plan.event.start.astimezone(UTC) < today_end),
            key=lambda plan: (plan.event.start.astimezone(UTC), plan.event.id),
        )

    def delete_old_plans(self, before: datetime) -> int:
        """Delete plans with event_start before given datetime. Returns count deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM plans WHERE event_start < ?", (before.isoformat(),)
            )
            return cursor.rowcount

    def realtime_refresh_plans(
        self, now: datetime, *, horizon_minutes: int, grace_seconds: int,
    ) -> list[Plan]:
        """Bounded upcoming plans with an actionable pending alarm.

        Compare instants in SQLite, not ISO strings with differing offsets.
        Missing/deleted (cancelled) plans and completed alarms are not refreshed.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT plan_json FROM plans
                WHERE julianday(event_start) > julianday(?)
                  AND julianday(json_extract(plan_json, '$.leave_at'))
                      BETWEEN julianday(?) AND julianday(?)
                  AND EXISTS (SELECT 1 FROM pings WHERE pings.event_id = plans.event_id
                              AND fired = 0 AND kind IN ('prep', 'leave'))
                """,
                (now.isoformat(),
                 (now.astimezone(UTC) - timedelta(seconds=grace_seconds)).isoformat(),
                 (now.astimezone(UTC) + timedelta(minutes=horizon_minutes)).isoformat()),
            ).fetchall()
        return [Plan.model_validate_json(row[0]) for row in rows]

    def increase_realtime_buffer(
        self, expected: Plan, minutes: int, reason: str | None, now: datetime,
    ) -> bool:
        """CAS the observed plan and shift pending alarms in one write transaction.

        No row recreation: IDs, fired state and retry counts survive. A concurrent
        refresh/manual edit/replan invalidates the observation rather than applying
        a stale delta. Pending ping offsets (including snoozes) are preserved;
        newly urgent alarms clamp to now, already-due alarms are never postponed.
        """
        from commutecompass.format import format_leave_ping, format_prep_ping

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT plan_json FROM plans WHERE event_id = ?", (expected.event.id,),
            ).fetchone()
            if row is None:
                return False
            current = Plan.model_validate_json(row[0])
            if (not _same_plan(current, expected) or current.error is not None or current.leave_at is None
                    or current.event.start.astimezone(UTC) <= now.astimezone(UTC)
                    or minutes <= current.realtime_buffer_minutes):
                return False
            delta = timedelta(minutes=minutes - current.realtime_buffer_minutes)

            def advance(value: datetime) -> datetime:
                return (value.astimezone(UTC) - delta).astimezone(value.tzinfo)

            updated = current.model_copy(update={
                "leave_at": advance(current.leave_at),
                "prep_at": advance(current.prep_at) if current.prep_at is not None else None,
                "realtime_buffer_minutes": minutes, "realtime_reason": reason,
                "leave_buffer_minutes": (
                    current.leave_buffer_minutes + minutes - current.realtime_buffer_minutes
                    if current.leave_buffer_minutes is not None else None
                ),
            })
            conn.execute(
                "UPDATE plans SET plan_json = ?, planned_at = ? WHERE event_id = ?",
                (_json_dumps(updated.model_dump()), now.isoformat(), current.event.id),
            )
            pings = conn.execute(
                "SELECT id, kind, fire_at FROM pings WHERE event_id = ? AND fired = 0 "
                "AND kind IN ('prep', 'leave')", (current.event.id,),
            ).fetchall()
            for ping_id, kind, raw_time in pings:
                old_time = datetime.fromisoformat(raw_time).astimezone(UTC)
                fire_at = min(old_time, max(now.astimezone(UTC), old_time - delta))
                message = format_leave_ping(updated) if kind == "leave" else format_prep_ping(updated)
                conn.execute(
                    "UPDATE pings SET fire_at = ?, message = ? WHERE id = ? AND fired = 0",
                    (fire_at.astimezone(now.tzinfo).isoformat(), message, ping_id),
                )
            return True

    @staticmethod
    def _obsolete_prep(conn: sqlite3.Connection, plan: Plan | None,
                       event_id: str, now: datetime) -> bool:
        """Read departure state under the caller's write lock, never a snapshot."""
        return bool(
            (plan is not None and plan.error == "too_imminent")
            or conn.execute(
                "SELECT 1 FROM pings WHERE event_id = ? AND kind = 'leave' "
                "AND retired = 0 AND (fired = 1 OR julianday(fire_at) <= julianday(?)) LIMIT 1",
                (event_id, now.isoformat()),
            ).fetchone()
        )

    def reconcile_poll_replan(
        self, expected: Plan, updated: Plan, now: datetime,
        *, alert_notification: tuple[str, str] | None = None,
    ) -> PollReplanOutcome | None:
        """Commit a poll replan plus actionable alarms under full-plan CAS.

        Preserve fired rows, IDs, retry counts and pending offsets from the old
        plan. Already-due alarms never move later; newly due alarms clamp to now.
        Both current and replanned events must still start strictly after now.
        Return the final actionable disposition, or None on stale rejection.
        No network work occurs while holding the write lock.
        """
        from commutecompass.format import format_leave_ping, format_prep_ping

        if updated.event.id != expected.event.id:
            return None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT plan_json FROM plans WHERE event_id = ?", (expected.event.id,),
            ).fetchone()
            if row is None:
                return None
            current = Plan.model_validate_json(row[0])
            if (not _same_plan(current, expected)
                    or current.event.start.astimezone(UTC) <= now.astimezone(UTC)
                    or updated.event.start.astimezone(UTC) <= now.astimezone(UTC)):
                return None
            conn.execute(
                "UPDATE plans SET plan_json = ?, planned_at = ?, event_start = ? WHERE event_id = ?",
                (_json_dumps(updated.model_dump()), now.isoformat(),
                 updated.event.start.isoformat(), expected.event.id),
            )
            for kind, old_time, new_time, formatter in (
                ("prep", expected.prep_at, updated.prep_at, format_prep_ping),
                ("leave", expected.leave_at, updated.leave_at, format_leave_ping),
            ):
                rows = conn.execute(
                    "SELECT id, fire_at, fired FROM pings WHERE event_id = ? AND kind = ? "
                    "AND retired = 0",
                    (expected.event.id, kind),
                ).fetchall()
                if new_time is None:
                    continue  # Never discard a pending actionable alarm on failure.
                message = formatter(updated)
                for ping_id, raw_time, fired in rows:
                    if fired:
                        continue
                    fire_at = datetime.fromisoformat(raw_time).astimezone(UTC)
                    if old_time is not None:
                        shifted = fire_at + (new_time.astimezone(UTC) - old_time.astimezone(UTC))
                    else:
                        shifted = new_time.astimezone(UTC)
                    safe_time = (min(fire_at, shifted) if fire_at <= now.astimezone(UTC)
                                 else max(now.astimezone(UTC), shifted))
                    conn.execute(
                        "UPDATE pings SET fire_at = ?, message = ? WHERE id = ? AND fired = 0",
                        (safe_time.astimezone(now.tzinfo).isoformat(), message, ping_id),
                    )
                if not rows:
                    fire_at = max(now.astimezone(UTC), new_time.astimezone(UTC))
                    conn.execute(
                        "INSERT INTO pings (id, event_id, kind, fire_at, message) VALUES (?, ?, ?, ?, ?)",
                        (str(uuid4()), expected.event.id, kind,
                         fire_at.astimezone(now.tzinfo).isoformat(), message),
                    )
            # Final row deadlines include preserved user offsets. Decide under
            # the same lock as reconciliation, not with a post-commit query.
            obsolete_prep = self._obsolete_prep(conn, updated, expected.event.id, now)
            due = conn.execute(
                "SELECT kind FROM pings WHERE event_id = ? AND fired = 0 "
                "AND kind IN ('prep', 'leave') AND julianday(fire_at) <= julianday(?)",
                (expected.event.id, now.isoformat()),
            ).fetchall()
            consumed = conn.execute(
                "SELECT 1 FROM pings WHERE event_id = ? AND kind = 'leave' AND fired = 1 "
                "AND retired = 0 "
                "AND julianday(fire_at) <= julianday(?) LIMIT 1",
                (expected.event.id, now.isoformat()),
            ).fetchone()
            if alert_notification is not None:
                alert_id, message = alert_notification
                day_start, _ = _logical_day_window(now)
                expires = min(
                    updated.event.start.astimezone(UTC),
                    now.astimezone(UTC) + timedelta(seconds=ALERT_NOTIFICATION_GRACE_SECONDS),
                )
                conn.execute(
                    """INSERT INTO alert_notifications
                       (alert_id, event_id, day_start, message, expires_at)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (alert_id, updated.event.id, day_start.isoformat(), message, expires.isoformat()),
                )
            return PollReplanOutcome(
                actionable_due=any(row[0] == "leave" or not obsolete_prep for row in due),
                imminent_leave_consumed=consumed is not None,
            )

    # ── Ping CRUD ──────────────────────────────────────────────────────────────

    def schedule_ping(self, ping: PingEntry) -> None:
        """Supersede a ping generation, retaining consumed and retry history.

        Retirement survives replacement dispatch/cancellation before an old claim
        fails, so releasing that old claim can never resurrect it.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._retire_pings(conn, ping.event_id, ping.kind)
            conn.execute(
                """
                INSERT INTO pings (
                    id, event_id, kind, fire_at, fired, fired_at, message, send_attempts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ping.id,
                    ping.event_id,
                    ping.kind,
                    ping.fire_at.isoformat(),
                    1 if ping.fired else 0,
                    ping.fired_at.isoformat() if ping.fired_at else None,
                    ping.message,
                    ping.send_attempts,
                ),
            )

    def cancel_pings(self, event_id: str) -> int:
        """Retire current pings, including claims; keep fired and retry history."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = self._retire_pings(conn, event_id)
            conn.execute("DELETE FROM alert_notifications WHERE event_id = ? AND delivered_at IS NULL",
                         (event_id,))
            return count

    @staticmethod
    def _retire_pings(conn: sqlite3.Connection, event_id: str, kind: str | None = None) -> int:
        cursor = conn.execute(
            "UPDATE pings SET retired = 1, fired = 1 WHERE event_id = ? AND retired = 0"
            + (" AND kind = ?" if kind is not None else ""),
            (event_id, kind) if kind is not None else (event_id,),
        )
        return cursor.rowcount

    @staticmethod
    def _matches_expected(conn: sqlite3.Connection, event_id: str, expected: Plan | None) -> bool:
        row = conn.execute("SELECT plan_json FROM plans WHERE event_id = ?", (event_id,)).fetchone()
        return (row is None if expected is None else
                row is not None and _same_plan(Plan.model_validate_json(row[0]), expected))

    def remove_plan_if_current(self, expected: Plan, now: datetime) -> bool:
        """Invalidate a confirmed-absent snapshot, never a newer replacement."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._matches_expected(conn, expected.event.id, expected):
                return False
            self._retire_pings(conn, expected.event.id)
            conn.execute("DELETE FROM plans WHERE event_id = ?", (expected.event.id,))
            conn.execute("DELETE FROM alert_notifications WHERE event_id = ? AND delivered_at IS NULL",
                         (expected.event.id,))
            return True

    def reconcile_morning_plan(
        self, plan: Plan, now: datetime, *, expected: Plan | None,
    ) -> bool:
        """CAS the plan and per-kind reminders, preserving last-good on outage."""
        from commutecompass.format import format_leave_ping, format_prep_ping

        instant = now.astimezone(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._matches_expected(conn, plan.event.id, expected):
                return False
            # A virtual calendar event may have a physical location override.
            # The planned shape, not the calendar flag, determines its commute.
            transient = plan.error is not None and plan.error != "too_imminent"
            if transient and plan.event.start.astimezone(UTC) > instant and expected is not None:
                return True  # Keep last-good route and reminders on transient failure.
            conn.execute(
                """INSERT INTO plans (event_id, plan_json, planned_at, event_start)
                   VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET
                   plan_json = excluded.plan_json, planned_at = excluded.planned_at,
                   event_start = excluded.event_start""",
                (plan.event.id, _json_dumps(plan.model_dump()), now.isoformat(),
                 plan.event.start.isoformat()),
            )
            if transient and plan.event.start.astimezone(UTC) > instant:
                return True
            for kind, deadline, old_deadline, formatter in (
                ("prep", plan.prep_at, expected.prep_at if expected else None, format_prep_ping),
                ("leave", plan.leave_at, expected.leave_at if expected else None, format_leave_ping),
            ):
                unchanged = (deadline is not None and old_deadline is not None
                             and deadline.astimezone(UTC) == old_deadline.astimezone(UTC))
                obsolete = (plan.event.start.astimezone(UTC) <= instant
                            or deadline is None or (kind == "prep" and plan.error == "too_imminent"))
                if obsolete or (not unchanged and deadline is not None
                                and deadline.astimezone(UTC) < instant):
                    self._retire_pings(conn, plan.event.id, kind)
                    continue
                current = conn.execute(
                    "SELECT id, fired FROM pings WHERE event_id = ? AND kind = ? AND retired = 0",
                    (plan.event.id, kind),
                ).fetchall()
                assert deadline is not None
                if unchanged and current:
                    # Do not consume/retire an unchanged due retry, nor recreate a
                    # consumed generation. A pending retry keeps its attempts.
                    conn.execute(
                        "UPDATE pings SET message = ? WHERE event_id = ? AND kind = ? "
                        "AND retired = 0 AND fired = 0", (formatter(plan), plan.event.id, kind),
                    )
                    continue
                if deadline.astimezone(UTC) < instant:
                    continue  # Never manufacture a missed morning alarm.
                self._retire_pings(conn, plan.event.id, kind)
                conn.execute(
                    "INSERT INTO pings (id, event_id, kind, fire_at, message) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid4()), plan.event.id, kind, deadline.isoformat(), formatter(plan)),
                )
            return True

    def pending_pings(self, before: datetime) -> list[PingEntry]:
        """Return all unfired pings with fire_at <= before."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message, send_attempts
                FROM pings
                WHERE fired = 0 AND julianday(fire_at) <= julianday(?)
                ORDER BY julianday(fire_at)
                """,
                (before.isoformat(),),
            ).fetchall()

        pings = []
        for row in rows:
            pings.append(
                PingEntry(
                    id=row[0],
                    event_id=row[1],
                    kind=row[2],
                    fire_at=datetime.fromisoformat(row[3]),
                    fired=bool(row[4]),
                    fired_at=datetime.fromisoformat(row[5]) if row[5] else None,
                    message=row[6],
                    send_attempts=row[7],
                )
            )
        return pings

    def mark_fired(self, ping_id: str, fired_at: datetime) -> None:
        """Mark a ping as fired (unconditional; prefer ``claim_ping``)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE pings SET fired = 1, fired_at = ? WHERE id = ?",
                (fired_at.isoformat(), ping_id),
            )

    def claim_ping(self, ping_id: str, fired_at: datetime) -> bool:
        """Atomically claim a ping iff it has not yet been fired.

        Returns True when the caller successfully transitioned ``fired = 0 -> 1``
        and should now send the message.  Returns False if another concurrent
        caller (or a previous run) already claimed it — in which case the
        caller MUST NOT send, to avoid duplicate notifications.

        Marking happens *before* the network send so two concurrent runners
        cannot both send.  If the send then fails, the caller may hand the row
        back with ``release_ping`` so a later poll re-attempts it (bounded by an
        attempt cap + grace window).  Observability is provided by the caller
        (log + summary line).
        """
        return self.claim_ping_entry(ping_id, fired_at) is not None

    def claim_ping_entry(
        self, ping_id: str, fired_at: datetime, *, leave_only: bool = False,
        due_only: bool = False,
        defer_if: Callable[[Plan | None, CurrentLocation | None], bool] | None = None,
        suppress_obsolete_prep: bool = False,
    ) -> PingEntry | None:
        """Atomically claim and return the authoritative current ping payload.

        Returns None if absent, already claimed, or suppressed by ``leave_only``.
        The write lock covers both the 0→1 transition and payload read: a
        concurrent refresh/replan cannot leave the caller sending an old snapshot.
        Dispatch must use this returned row, not a prior ``pending_pings`` row.
        ``leave_only`` applies quiet-hours filtering to the current kind without
        consuming suppressed rows. ``due_only`` requires the current fire_at
        instant to be <= fired_at, atomically; future rows remain untouched.
        Both filters default to False for compatibility with unconditional
        unfired-row claims (including ``claim_ping``). Retry semantics are unchanged.
        For eligible prep/leave rows only, optional synchronous ``defer_if`` reads
        the current plan and unaged location under the same write lock. True
        leaves every ping field unchanged unless the event is muted (mute still
        consumes rows). The callback must not perform I/O;
        it may retain the supplied plan for authoritative post-claim expiry.
        Poll-only ``suppress_obsolete_prep`` retires eligible prep when the current
        plan is too imminent or a current leave is due/consumed. Deferral and
        quiet-hours eligibility take precedence; leave claims never consume prep.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if defer_if is not None or suppress_obsolete_prep:
                eligible = conn.execute(
                    "SELECT event_id, kind FROM pings WHERE id = ? AND fired = 0 "
                    "AND (? = 0 OR kind = 'leave') "
                    "AND (? = 0 OR julianday(fire_at) <= julianday(?))",
                    (ping_id, int(leave_only), int(due_only), fired_at.isoformat()),
                ).fetchone()
                if eligible is None:
                    return None
                if eligible[1] in {"prep", "leave"}:
                    plan_row = conn.execute(
                        "SELECT plan_json FROM plans WHERE event_id = ?", (eligible[0],),
                    ).fetchone()
                    plan = Plan.model_validate(_json_loads(plan_row[0])) if plan_row else None
                    location = self._current_location_from_conn(conn)
                    defer = defer_if(plan, location) if defer_if is not None else False
                    muted = conn.execute(
                        "SELECT 1 FROM event_mutes WHERE event_id = ? "
                        "AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))",
                        (eligible[0], fired_at.isoformat()),
                    ).fetchone()
                    if defer and muted is None:
                        return None
                    if (suppress_obsolete_prep and eligible[1] == "prep"
                            and self._obsolete_prep(conn, plan, eligible[0], fired_at)):
                        conn.execute(
                            "UPDATE pings SET fired = 1, fired_at = ? WHERE id = ? AND fired = 0",
                            (fired_at.isoformat(), ping_id),
                        )
                        return None
            cursor = conn.execute(
                "UPDATE pings SET fired = 1, fired_at = ? WHERE id = ? AND fired = 0 "
                "AND (? = 0 OR kind = 'leave') "
                "AND (? = 0 OR julianday(fire_at) <= julianday(?))",
                (fired_at.isoformat(), ping_id, int(leave_only), int(due_only),
                 fired_at.isoformat()),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT id, event_id, kind, fire_at, fired, fired_at, message, send_attempts "
                "FROM pings WHERE id = ?", (ping_id,),
            ).fetchone()
            return PingEntry(
                id=row[0], event_id=row[1], kind=row[2],
                fire_at=datetime.fromisoformat(row[3]), fired=bool(row[4]),
                fired_at=datetime.fromisoformat(row[5]) if row[5] else None,
                message=row[6], send_attempts=row[7],
            )

    def release_ping(self, ping_id: str) -> bool:
        """Hand a claimed ping back to the unfired pool after a failed send.

        Atomically transitions ``fired = 1 -> 0`` and increments
        ``send_attempts`` so the next poll picks the row up again.  Returns True
        only when the row was actually claimed (``fired = 1``); a row already
        re-fired or never claimed is left untouched.  The caller is responsible
        for bounding re-fire (attempt cap + grace window) so this can never
        storm.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pings
                SET fired = 0, fired_at = NULL, send_attempts = send_attempts + 1
                WHERE id = ? AND fired = 1 AND retired = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM pings newer WHERE newer.event_id = pings.event_id
                    AND newer.kind = pings.kind AND newer.id != pings.id
                    AND newer.fired = 0
                  )
                """,
                (ping_id,),
            )
            return cursor.rowcount == 1

    # ── Geocode cache ───────────────────────────────────────────────────────────

    def cache_geocode(self, raw: str, resolved: ResolvedLocation) -> None:
        """Cache a geocode result."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO geocode_cache (raw, resolved_json, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(raw) DO UPDATE SET
                    resolved_json = excluded.resolved_json,
                    cached_at = excluded.cached_at
                """,
                (raw, _json_dumps(resolved.model_dump()), _now_iso()),
            )

    def get_geocode(self, raw: str, max_age_days: int = 30) -> ResolvedLocation | None:
        """Retrieve a cached geocode result if it exists and is fresh."""
        from datetime import timedelta

        from commutecompass.timeutil import now_nyc

        cutoff = now_nyc() - timedelta(days=max_age_days)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT resolved_json FROM geocode_cache
                WHERE raw = ? AND cached_at >= ?
                """,
                (raw, cutoff.isoformat()),
            ).fetchone()

        if row is None:
            return None
        data = _json_loads(row[0])
        return ResolvedLocation.model_validate(data)

    # ── Route cache ──────────────────────────────────────────────────────────────

    def cache_route(self, origin_key: str, dest_value: str, mode: str, route: Route) -> None:
        """Store the latest successful route for an (origin, dest, mode) triple.

        Used as a fallback when live routing is unavailable.  Only one row per
        triple is kept (latest wins); the cached travel duration is what lets
        the planner still compute a leave time during an API outage.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO route_cache (origin_key, dest_value, mode, route_json, cached_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(origin_key, dest_value, mode) DO UPDATE SET
                    route_json = excluded.route_json,
                    cached_at = excluded.cached_at
                """,
                (origin_key, dest_value, mode, _json_dumps(route.model_dump()), _now_iso()),
            )

    def get_cached_route(
        self, origin_key: str, dest_value: str, mode: str, max_age_days: int = 30
    ) -> Route | None:
        """Return the most recent cached route for the triple, if fresh enough."""
        from datetime import timedelta

        from commutecompass.timeutil import now_nyc

        cutoff = now_nyc() - timedelta(days=max_age_days)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT route_json FROM route_cache
                WHERE origin_key = ? AND dest_value = ? AND mode = ? AND cached_at >= ?
                """,
                (origin_key, dest_value, mode, cutoff.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return Route.model_validate(_json_loads(row[0]))

    # ── Job heartbeat ────────────────────────────────────────────────────────────

    def record_job_success(self, job: str, at: datetime) -> None:
        """Record the most recent successful completion of a named job."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_heartbeat (job, last_success)
                VALUES (?, ?)
                ON CONFLICT(job) DO UPDATE SET last_success = excluded.last_success
                """,
                (job, at.isoformat()),
            )

    def get_job_heartbeat(self, job: str) -> datetime | None:
        """Return the last successful run time for a job, or None if never run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_success FROM job_heartbeat WHERE job = ?", (job,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0])

    # ── Alert ledger ────────────────────────────────────────────────────────────

    def mark_alert_seen(self, alert_id: str, event_id: str) -> None:
        """Record that an alert has been seen for a given event."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alerts_seen (alert_id, event_id, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alert_id, event_id) DO NOTHING
                """,
                (alert_id, event_id, _now_iso()),
            )

    def is_alert_seen(self, alert_id: str, event_id: str) -> bool:
        """Return True if this alert has been seen for this event."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts_seen WHERE alert_id = ? AND event_id = ? LIMIT 1",
                (alert_id, event_id),
            ).fetchone()
        return row is not None

    def is_alert_seen_today(self, alert_id: str) -> bool:
        """Only acknowledged delivery, never legacy processing, is announced."""
        day_start, _ = _logical_day_window()
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM alert_notifications WHERE alert_id = ? AND day_start = ? "
                "AND delivered_at IS NOT NULL LIMIT 1", (alert_id, day_start.isoformat()),
            ).fetchone() is not None

    def claim_alert_processing(self, alert_id: str, event_id: str, now: datetime) -> str | None:
        """Bound failed replan attempts without calling an exception processed."""
        instant = now.astimezone(UTC)
        token = str(uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM alerts_seen WHERE alert_id = ? AND event_id = ?",
                            (alert_id, event_id)).fetchone():
                return None
            conn.execute(
                "INSERT INTO alert_processing (alert_id, event_id, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (alert_id, event_id,
                 (instant + timedelta(seconds=ALERT_NOTIFICATION_GRACE_SECONDS)).isoformat()),
            )
            changed = conn.execute(
                """UPDATE alert_processing SET attempts = attempts + 1, claim_token = ?,
                   claimed_until = ? WHERE alert_id = ? AND event_id = ? AND attempts < ?
                   AND julianday(expires_at) > julianday(?) AND
                   (claimed_until IS NULL OR julianday(claimed_until) <= julianday(?))""",
                (token, (instant + timedelta(seconds=ALERT_NOTIFICATION_LEASE_SECONDS)).isoformat(),
                 alert_id, event_id, ALERT_NOTIFICATION_MAX_ATTEMPTS,
                 instant.isoformat(), instant.isoformat()),
            ).rowcount
            return token if changed else None

    def finish_alert_processing(
        self, alert_id: str, event_id: str, token: str, now: datetime, *, processed: bool,
        expected: Plan | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected is not None and not self._matches_expected(conn, event_id, expected):
                processed = False
            changed = conn.execute(
                "UPDATE alert_processing SET claim_token = NULL, claimed_until = NULL "
                "WHERE alert_id = ? AND event_id = ? AND claim_token = ?",
                (alert_id, event_id, token),
            ).rowcount
            if changed and processed:
                conn.execute(
                    "INSERT INTO alerts_seen (alert_id, event_id, seen_at) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING", (alert_id, event_id, now.isoformat()),
                )

    def pending_alert_notifications(self, now: datetime) -> list[tuple[str, str, str]]:
        """List retryable intents; claims recheck expiry and cancellation."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alert_id, event_id, day_start FROM alert_notifications "
                "WHERE delivered_at IS NULL AND attempts < ? "
                "AND julianday(expires_at) > julianday(?) ORDER BY day_start, alert_id, event_id",
                (ALERT_NOTIFICATION_MAX_ATTEMPTS, now.isoformat()),
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def claim_alert_notification(
        self, alert_id: str, event_id: str, day_start: str, now: datetime,
    ) -> tuple[str, str] | None:
        """Lease an alert/day announcement, not an exactly-once send guarantee.

        Acceptance before local acknowledgement can duplicate after lease expiry.
        """
        instant = now.astimezone(UTC)
        token = str(uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plan_row = conn.execute("SELECT plan_json FROM plans WHERE event_id = ?",
                                    (event_id,)).fetchone()
            if plan_row is None:
                return None
            plan = Plan.model_validate_json(plan_row[0])
            if plan.event.start.astimezone(UTC) <= instant:
                return None
            other = conn.execute(
                """SELECT 1 FROM alert_notifications WHERE alert_id = ? AND day_start = ?
                   AND (delivered_at IS NOT NULL OR julianday(claimed_until) > julianday(?))
                   LIMIT 1""", (alert_id, day_start, instant.isoformat()),
            ).fetchone()
            if other is not None:
                return None
            row = conn.execute(
                """UPDATE alert_notifications SET attempts = attempts + 1, claim_token = ?,
                   claimed_until = ? WHERE alert_id = ? AND event_id = ? AND day_start = ?
                   AND delivered_at IS NULL AND attempts < ?
                   AND julianday(expires_at) > julianday(?) RETURNING message""",
                (token, (instant + timedelta(seconds=ALERT_NOTIFICATION_LEASE_SECONDS)).isoformat(),
                 alert_id, event_id, day_start, ALERT_NOTIFICATION_MAX_ATTEMPTS, instant.isoformat()),
            ).fetchone()
            return (token, row[0]) if row else None

    def finish_alert_notification(
        self, alert_id: str, event_id: str, day_start: str, token: str, now: datetime,
        *, delivered: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE alert_notifications SET claim_token = NULL, claimed_until = NULL,
                   delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END
                   WHERE alert_id = ? AND event_id = ? AND day_start = ? AND claim_token = ?""",
                (delivered, now.isoformat(), alert_id, event_id, day_start, token),
            )

    # ── Diagnostics ────────────────────────────────────────────────────────────

    def all_pings_today(self) -> list[PingEntry]:
        """Return current-day ping history, ordered by instant then stable ID."""
        day_start, day_end = _logical_day_window()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, event_id, kind, fire_at, fired, fired_at, message, send_attempts FROM pings",
            ).fetchall()
        pings = [PingEntry(
            id=row[0], event_id=row[1], kind=row[2], fire_at=datetime.fromisoformat(row[3]),
            fired=bool(row[4]), fired_at=datetime.fromisoformat(row[5]) if row[5] else None,
            message=row[6], send_attempts=row[7],
        ) for row in rows]
        return sorted(
            (ping for ping in pings if day_start <= ping.fire_at.astimezone(UTC) < day_end),
            key=lambda ping: (ping.fire_at.astimezone(UTC), ping.id),
        )

    def geocode_cache_stats(self) -> dict[str, Any]:
        """Return summary stats for the geocode cache: count + oldest/newest age."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(cached_at), MAX(cached_at) FROM geocode_cache"
            ).fetchone()
        count, oldest, newest = row[0], row[1], row[2]
        return {"count": count, "oldest_cached_at": oldest, "newest_cached_at": newest}

    def geocode_cache_list(self) -> list[dict[str, str]]:
        """Return all geocode cache entries (raw query + cached timestamp)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT raw, cached_at FROM geocode_cache ORDER BY cached_at DESC"
            ).fetchall()
        return [{"raw": r[0], "cached_at": r[1]} for r in rows]

    def geocode_cache_invalidate(self, raw: str) -> int:
        """Delete a cached geocode entry; returns rows removed (0 or 1)."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM geocode_cache WHERE raw = ?", (raw,))
            return cursor.rowcount

    # ── Adjust idempotency log ──────────────────────────────────────────────────

    def record_adjust_key(self, key: str, event_id: str) -> bool:
        """Record an adjustment idempotency key; return True if new, False if dup.

        OpenClaw (or any agent caller) may retry a flaky ``adjust`` invocation;
        without dedup the prep time shifts on every retry.  Callers pass a
        stable key (e.g. an upstream correlation id or a derived hash) — the
        first call writes it and returns True, subsequent calls return False
        and the CLI no-ops with exit 0.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO adjust_log (key, event_id, applied_at) "
                "VALUES (?, ?, ?)",
                (key, event_id, _now_iso()),
            )
            return cursor.rowcount == 1

    def record_adjust(
        self,
        event_id: str,
        add_prep_minutes: int,
        prev_prep_at: datetime,
        *,
        key: str | None = None,
    ) -> str | None:
        """Record a full adjustment row so ``undo`` can restore the prior prep_at.

        When ``key`` is provided, behaves like ``record_adjust_key`` w.r.t.
        deduplication — a previously-seen key returns ``None`` (caller no-ops).
        When ``key`` is ``None``, an ``auto:<uuid>`` key is generated so the row
        is uniquely addressable but the caller doesn't have to invent one.
        """
        import uuid

        actual_key = key if key is not None else f"auto:{uuid.uuid4()}"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO adjust_log
                    (key, event_id, applied_at, add_prep_minutes, prev_prep_at, undone)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    actual_key,
                    event_id,
                    _now_iso(),
                    add_prep_minutes,
                    prev_prep_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return actual_key

    def last_adjust(self, event_id: str | None = None) -> AdjustRow | None:
        """Return the most recent non-undone adjust row, optionally filtered to one event.

        ``undo`` calls this to find the row to revert.  Rows where
        ``add_prep_minutes`` is NULL (created by the legacy
        ``record_adjust_key`` path before the schema was extended) are skipped
        — there's no recorded ``prev_prep_at`` to restore.
        """
        sql = (
            "SELECT key, event_id, applied_at, add_prep_minutes, prev_prep_at, undone "
            "FROM adjust_log "
            "WHERE undone = 0 AND add_prep_minutes IS NOT NULL AND prev_prep_at IS NOT NULL "
        )
        params: tuple[Any, ...] = ()
        if event_id is not None:
            sql += "AND event_id = ? "
            params = (event_id,)
        sql += "ORDER BY applied_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return AdjustRow(
            key=row[0],
            event_id=row[1],
            applied_at=datetime.fromisoformat(row[2]),
            add_prep_minutes=int(row[3]),
            prev_prep_at=datetime.fromisoformat(row[4]),
            undone=bool(row[5]),
        )

    def mark_adjust_undone(self, key: str) -> bool:
        """Flip ``undone=1`` for the named row; return True if a row was updated."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE adjust_log SET undone = 1 WHERE key = ? AND undone = 0",
                (key,),
            )
            return cursor.rowcount == 1

    # ── Mute ledger ────────────────────────────────────────────────────────────

    def mute_event(
        self, event_id: str, *, expires_at: datetime | None = None
    ) -> None:
        """Suppress all pings for ``event_id`` until ``expires_at`` (or forever).

        Enforced in ``jobs.poll`` before notifying on a claimed ping — see the
        ``is_muted`` check there.  Idempotent: re-muting the same event with a
        new expiry just updates the row.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_mutes (event_id, muted_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    muted_at = excluded.muted_at,
                    expires_at = excluded.expires_at
                """,
                (
                    event_id,
                    _now_iso(),
                    expires_at.isoformat() if expires_at else None,
                ),
            )

    def unmute_event(self, event_id: str) -> int:
        """Remove the mute record for ``event_id``; returns rows removed (0 or 1)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM event_mutes WHERE event_id = ?", (event_id,)
            )
            return cursor.rowcount

    def is_muted(self, event_id: str) -> bool:
        """Return True if ``event_id`` has a non-expired mute record.

        ``expires_at`` IS NULL means "forever"; otherwise the mute is active
        only while ``now() < expires_at``.  Past-expiry rows are left in place
        for inspection but report as not muted.
        """
        from commutecompass.timeutil import now_nyc

        with self._connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM event_mutes WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return False
        expires_at = row[0]
        if expires_at is None:
            return True
        return now_nyc() < datetime.fromisoformat(expires_at)

    # ── Pending-ping lookup ────────────────────────────────────────────────────

    def get_pending_ping(self, event_id: str, kind: str) -> PingEntry | None:
        """Return the unfired ping (if any) for ``(event_id, kind)``.

        Used by ``snooze`` to look up the ping it's about to shift or skip.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message, send_attempts
                FROM pings
                WHERE event_id = ? AND kind = ? AND fired = 0
                """,
                (event_id, kind),
            ).fetchone()
        if row is None:
            return None
        return PingEntry(
            id=row[0],
            event_id=row[1],
            kind=row[2],
            fire_at=datetime.fromisoformat(row[3]),
            fired=bool(row[4]),
            fired_at=datetime.fromisoformat(row[5]) if row[5] else None,
            message=row[6],
            send_attempts=row[7],
        )

    # ── Current location (singleton) ────────────────────────────────────────────

    def upsert_current_location(self, loc: CurrentLocation) -> None:
        """Insert or replace the singleton current_location row."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO current_location (id, lat, lon, zone, captured_at, source, accuracy_m)
                VALUES ('singleton', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lat = excluded.lat,
                    lon = excluded.lon,
                    zone = excluded.zone,
                    captured_at = excluded.captured_at,
                    source = excluded.source,
                    accuracy_m = excluded.accuracy_m
                """,
                (
                    loc.lat,
                    loc.lon,
                    loc.zone,
                    loc.captured_at.isoformat(),
                    loc.source,
                    loc.accuracy_m,
                ),
            )

    def get_current_location(
        self, max_age_minutes: int | None = None
    ) -> CurrentLocation | None:
        """Return the singleton current_location if it exists.

        When max_age_minutes is set, return None if the row is older than that.
        """
        with self._connect() as conn:
            location = self._current_location_from_conn(conn)
        if location is not None and max_age_minutes is not None:
            from commutecompass.timeutil import now_nyc

            if location.captured_at.utcoffset() is None:
                return None
            age = now_nyc().astimezone(UTC) - location.captured_at.astimezone(UTC)
            if age.total_seconds() > max_age_minutes * 60:
                return None
        return location

    @staticmethod
    def _current_location_from_conn(conn: sqlite3.Connection) -> CurrentLocation | None:
        """Read the unaged fix, including inside an existing claim transaction."""
        row = conn.execute(
            "SELECT lat, lon, zone, captured_at, source, accuracy_m "
            "FROM current_location WHERE id = 'singleton'"
        ).fetchone()
        if row is None:
            return None
        try:
            return CurrentLocation(
                lat=row[0], lon=row[1], zone=row[2],
                captured_at=datetime.fromisoformat(row[3]), source=row[4], accuracy_m=row[5],
            )
        except (ValueError, TypeError):
            return None
