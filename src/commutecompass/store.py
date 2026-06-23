"""SQLite persistence layer."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional, cast

import sqlite3

from commutecompass.models import (
    AdjustRow,
    CurrentLocation,
    Plan,
    PingEntry,
    ResolvedLocation,
    Route,
)


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
            # Ensure only one unfired ping per (event_id, kind).
            # Migrate existing duplicates first, keeping the row with the latest fire_at.
            cursor = conn.execute("PRAGMA table_info(pings)")
            columns = {row[1] for row in cursor.fetchall()}
            if "event_id" in columns and "kind" in columns:
                conn.execute("""
                    DELETE FROM pings WHERE rowid NOT IN (
                        SELECT MAX(rowid)
                        FROM pings
                        WHERE fired = 0
                        GROUP BY event_id, kind
                    )
                """)
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_ping_event_kind_unfired
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

    def get_plan(self, event_id: str) -> Optional[Plan]:
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
        from commutecompass.timeutil import logical_day_bounds_nyc

        today_start, today_end = logical_day_bounds_nyc()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT plan_json FROM plans
                WHERE event_start >= ? AND event_start <= ?
                ORDER BY event_start
                """,
                (today_start.isoformat(), today_end.isoformat()),
            ).fetchall()

        plans = []
        for row in rows:
            data = _json_loads(row[0])
            plans.append(Plan.model_validate(data))
        return plans

    def delete_old_plans(self, before: datetime) -> int:
        """Delete plans with event_start before given datetime. Returns count deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM plans WHERE event_start < ?", (before.isoformat(),)
            )
            return cursor.rowcount

    # ── Ping CRUD ──────────────────────────────────────────────────────────────

    def schedule_ping(self, ping: PingEntry) -> None:
        """Insert or replace a ping entry, ensuring at most one unfired ping per (event_id, kind).

        Re-scheduling a ping for the same (event_id, kind) replaces any existing unfired row.
        """
        with self._connect() as conn:
            # Remove any existing unfired ping for the same (event_id, kind) first,
            # then insert the new ping.  Using INSERT OR REPLACE would clobber the id
            # which we want to keep from the caller's uuid, so we do it explicitly.
            conn.execute(
                "DELETE FROM pings WHERE event_id = ? AND kind = ? AND fired = 0",
                (ping.event_id, ping.kind),
            )
            conn.execute(
                """
                INSERT INTO pings (id, event_id, kind, fire_at, fired, fired_at, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ping.id,
                    ping.event_id,
                    ping.kind,
                    ping.fire_at.isoformat(),
                    1 if ping.fired else 0,
                    ping.fired_at.isoformat() if ping.fired_at else None,
                    ping.message,
                ),
            )

    def cancel_pings(self, event_id: str) -> int:
        """Delete all pings for an event. Returns count cancelled."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pings WHERE event_id = ?", (event_id,)
            )
            return cursor.rowcount

    def pending_pings(self, before: datetime) -> list[PingEntry]:
        """Return all unfired pings with fire_at <= before."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message, send_attempts
                FROM pings
                WHERE fired = 0 AND fire_at <= ?
                ORDER BY fire_at
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
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE pings SET fired = 1, fired_at = ? WHERE id = ? AND fired = 0",
                (fired_at.isoformat(), ping_id),
            )
            return cursor.rowcount == 1

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
                WHERE id = ? AND fired = 1
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

    def get_geocode(self, raw: str, max_age_days: int = 30) -> Optional[ResolvedLocation]:
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
    ) -> Optional[Route]:
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
        """Return True if this alert was already announced today for any event.

        Used to deduplicate service_update messages so a single delayed train
        line that affects three of today's events doesn't generate three
        notifications.  The first event still triggers a message and the
        underlying replan; subsequent events replan silently.
        """
        from commutecompass.timeutil import logical_day_bounds_nyc

        day_start, _day_end = logical_day_bounds_nyc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts_seen WHERE alert_id = ? AND seen_at >= ? LIMIT 1",
                (alert_id, day_start.isoformat()),
            ).fetchone()
        return row is not None

    # ── Diagnostics ────────────────────────────────────────────────────────────

    def all_pings_today(self) -> list[PingEntry]:
        """Return every ping (fired or not) whose fire_at falls in today's logical day."""
        from commutecompass.timeutil import logical_day_bounds_nyc

        day_start, day_end = logical_day_bounds_nyc()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message
                FROM pings
                WHERE fire_at >= ? AND fire_at <= ?
                ORDER BY fire_at
                """,
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()

        return [
            PingEntry(
                id=row[0],
                event_id=row[1],
                kind=row[2],
                fire_at=datetime.fromisoformat(row[3]),
                fired=bool(row[4]),
                fired_at=datetime.fromisoformat(row[5]) if row[5] else None,
                message=row[6],
            )
            for row in rows
        ]

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
        key: Optional[str] = None,
    ) -> Optional[str]:
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

    def last_adjust(self, event_id: Optional[str] = None) -> Optional[AdjustRow]:
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
        self, event_id: str, *, expires_at: Optional[datetime] = None
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

    def get_pending_ping(self, event_id: str, kind: str) -> Optional[PingEntry]:
        """Return the unfired ping (if any) for ``(event_id, kind)``.

        Used by ``snooze`` to look up the ping it's about to shift or skip.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message
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
        self, max_age_minutes: Optional[int] = None
    ) -> Optional[CurrentLocation]:
        """Return the singleton current_location if it exists.

        When max_age_minutes is set, return None if the row is older than that.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lat, lon, zone, captured_at, source, accuracy_m "
                "FROM current_location WHERE id = 'singleton'"
            ).fetchone()
        if row is None:
            return None
        captured_at = datetime.fromisoformat(row[3])
        if max_age_minutes is not None:
            from commutecompass.timeutil import now_nyc

            age = now_nyc() - captured_at
            if age.total_seconds() > max_age_minutes * 60:
                return None
        return CurrentLocation(
            lat=row[0],
            lon=row[1],
            zone=row[2],
            captured_at=captured_at,
            source=row[4],
            accuracy_m=row[5],
        )
