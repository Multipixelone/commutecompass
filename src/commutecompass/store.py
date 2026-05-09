"""SQLite persistence layer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import sqlite3

from commutecompass.models import Alert, Plan, PingEntry, ResolvedLocation


def _json_dumps(obj: object) -> str:
    """Serialize an object to JSON, handling datetime offset format."""
    return json.dumps(obj, default=_json_serializer)


def _json_loads(raw: str) -> dict:
    """Parse a JSON string."""
    return json.loads(raw)


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

    def init_schema(self) -> None:
        """Create all tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
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
                CREATE TABLE IF NOT EXISTS alerts_seen (
                    alert_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (alert_id, event_id)
                );
            """)

    # ── Plan CRUD ──────────────────────────────────────────────────────────────

    def upsert_plan(self, plan: Plan) -> None:
        """Insert or replace a plan."""
        with sqlite3.connect(self.db_path) as conn:
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
                    datetime.now().isoformat(),
                    plan.event.start.isoformat(),
                ),
            )

    def get_plan(self, event_id: str) -> Optional[Plan]:
        """Retrieve a plan by event ID."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT plan_json FROM plans WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        data = _json_loads(row[0])
        return Plan.model_validate(data)

    def today_plans(self) -> list[Plan]:
        """Return all plans for today (event_start is today in America/New_York)."""
        from commutecompass.timeutil import NYC_TZ

        today_start = datetime.now(NYC_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)

        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM plans WHERE event_start < ?", (before.isoformat(),)
            )
            return cursor.rowcount

    # ── Ping CRUD ──────────────────────────────────────────────────────────────

    def schedule_ping(self, ping: PingEntry) -> None:
        """Insert a ping entry."""
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM pings WHERE event_id = ?", (event_id,)
            )
            return cursor.rowcount

    def pending_pings(self, before: datetime) -> list[PingEntry]:
        """Return all unfired pings with fire_at <= before."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, kind, fire_at, fired, fired_at, message
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
                )
            )
        return pings

    def mark_fired(self, ping_id: str, fired_at: datetime) -> None:
        """Mark a ping as fired."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE pings SET fired = 1, fired_at = ? WHERE id = ?",
                (fired_at.isoformat(), ping_id),
            )

    # ── Geocode cache ───────────────────────────────────────────────────────────

    def cache_geocode(self, raw: str, resolved: ResolvedLocation) -> None:
        """Cache a geocode result."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO geocode_cache (raw, resolved_json, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(raw) DO UPDATE SET
                    resolved_json = excluded.resolved_json,
                    cached_at = excluded.cached_at
                """,
                (raw, _json_dumps(resolved.model_dump()), datetime.now().isoformat()),
            )

    def get_geocode(self, raw: str, max_age_days: int = 30) -> Optional[ResolvedLocation]:
        """Retrieve a cached geocode result if it exists and is fresh."""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=max_age_days)

        with sqlite3.connect(self.db_path) as conn:
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

    # ── Alert ledger ────────────────────────────────────────────────────────────

    def mark_alert_seen(self, alert_id: str, event_id: str) -> None:
        """Record that an alert has been seen for a given event."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO alerts_seen (alert_id, event_id, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alert_id, event_id) DO NOTHING
                """,
                (alert_id, event_id, datetime.now().isoformat()),
            )

    def is_alert_seen(self, alert_id: str, event_id: str) -> bool:
        """Return True if this alert has been seen for this event."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts_seen WHERE alert_id = ? AND event_id = ? LIMIT 1",
                (alert_id, event_id),
            ).fetchone()
        return row is not None
