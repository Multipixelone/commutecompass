"""Static subway GTFS importer and read-only SQLite index (identity bridge).

See docs/realtime-identity-bridge.md §3, §4, §7 and §12 (D2, D3, D5, D6).
``gtfs-refresh`` downloads the MTA static subway feed, streams a *filtered*
import into a separate ``gtfs.db``, and swaps it in atomically.  The future
bridge (``gtfs_bridge.validate_leg``) reads it through :class:`GtfsIndex`.

Import (write side)
-------------------
``import_feed(zip_path, db_path, *, feed_kind, allowlist=None, source_url="",
now=None) -> ImportStats``
    Build a fresh index from a local GTFS zip.  Only trips on an allowlisted
    route that stop at that entry's allowlisted platform are kept, together
    with their **full** stop sequence (the ``num_stops`` check needs it).
    ``calendar``/``calendar_dates`` and an RT-trip_id table (every trip in
    the feed, for suffix-collision checks) are kept as well, plus all of
    ``stops.txt``.  The DB is built into a temp file next to ``db_path`` and
    ``os.replace``-d over it; any failure leaves the previous DB untouched.
    Raises ``AllowlistMismatchError`` when an allowlist parent/platform is
    missing, misnamed or further than ``max_coord_distance_m`` from the
    vendored coordinates, and ``FeedError`` for a broken/unusable feed.
    Any matched trip with repeated stops/sequences or malformed times rejects
    the entire import: dropping it could create a false unique boarding.

``refresh(db_path, *, allowlist=None, fetch=None, tmp_dir=None, now=None)
-> ImportStats``
    Download + import the supplemented feed; on any failure fall back to the
    regular feed (D5).  Raises ``GtfsRefreshError`` (``.config_error`` is
    True when an allowlist check failed) if neither succeeds.

Read API (bridge side)
----------------------
``GtfsIndex.open(path, *, now=None, max_age=MAX_INDEX_AGE) -> GtfsIndex | None``
    ``None`` when the index is missing, corrupt, of an unknown schema, older
    than ``max_age`` (8 days, D6), imported in the future, or when the NYC
    date of ``now`` is outside the feed's coverage.
``GtfsIndex.feed_info() -> FeedInfo``
``GtfsIndex.covers(service_date: date) -> bool``
``GtfsIndex.service_active(service_id: str, service_date: date) -> bool``
``GtfsIndex.departures_near(platform_stop_ids, route_ids, instant_utc,
window_s=30) -> list[Departure]``
    Every stop event at one of ``platform_stop_ids`` on one of ``route_ids``
    whose absolute departure instant is within ``window_s`` of
    ``instant_utc`` (tz-aware) and whose service is active on its service
    day.  Contributing service days are bounded by the indexed departure
    offsets and feed coverage.  Instants follow GTFS: seconds after "noon minus
    12h" of the service day (:func:`service_instant`), so ≥24:00:00 times and
    DST days are exact; all comparisons are in UTC.  Sorted by instant.
``GtfsIndex.trip_stops(trip_id) -> list[TripStop]`` — full stop sequence
    (with static stop name/coords/parent) of an imported trip, or ``[]``.
``GtfsIndex.stop(stop_id) -> StopInfo | None``
``GtfsIndex.rt_trip_id_unique(rt_trip_id, service_date) -> bool``
    True iff exactly one trip active on ``service_date`` has that RT trip_id
    (the static trip_id suffix after the first ``_``).
``GtfsIndex.close()``; also usable as a context manager.

Helpers: ``service_instant(service_date, offset_s) -> datetime`` (UTC),
``rt_trip_id(static_trip_id) -> str | None``, ``parse_gtfs_time(text) -> int``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import IO, Literal, Self

from commutecompass.gtfs_allowlist import (
    AllowlistEntry,
    haversine_m,
    load_allowlist,
    normalize_name,
)
from commutecompass.timeutil import NYC_TZ

_logger = logging.getLogger(__name__)

FeedKind = Literal["supplemented", "regular"]

SUPPLEMENTED_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip"
REGULAR_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
FEED_URLS: tuple[tuple[FeedKind, str], ...] = (
    ("supplemented", SUPPLEMENTED_URL),
    ("regular", REGULAR_URL),
)

# Versions 1/2 could silently drop matched trips/short rows, making uniqueness unsafe.
SCHEMA_VERSION = 3
MAX_INDEX_AGE = timedelta(days=8)
# Tolerate small clock skew between the refresh host and readers.
_FUTURE_SKEW = timedelta(minutes=5)
_DOWNLOAD_TIMEOUT_S = 120.0
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_REQUIRED_FILES = ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ─────────── Errors ───────────


class GtfsImportError(Exception):
    """Base class for import failures; the previous index is always kept."""


class FeedError(GtfsImportError):
    """The downloaded feed is missing, malformed or unusable."""


class AllowlistMismatchError(GtfsImportError):
    """An allowlist entry does not match the feed's stops.txt."""


class GtfsRefreshError(Exception):
    """Neither the supplemented nor the regular feed could be imported."""

    def __init__(self, message: str, *, config_error: bool) -> None:
        super().__init__(message)
        self.config_error = config_error


# ─────────── Result types ───────────


@dataclass(frozen=True)
class ImportStats:
    feed_kind: FeedKind
    feed_version: str
    feed_sha256: str
    coverage_start: date
    coverage_end: date
    imported_at: datetime
    trips: int
    stop_times: int
    rejected_trips: int


@dataclass(frozen=True)
class FeedInfo:
    feed_kind: FeedKind
    feed_version: str
    feed_sha256: str
    coverage_start: date
    coverage_end: date
    imported_at: datetime  # tz-aware UTC
    source_url: str


@dataclass(frozen=True)
class StopInfo:
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    parent_station: str | None
    location_type: int


@dataclass(frozen=True)
class Departure:
    trip_id: str
    rt_trip_id: str | None
    route_id: str
    service_id: str
    direction_id: int | None
    shape_id: str | None
    stop_id: str
    stop_sequence: int
    service_date: date
    arrival_s: int
    departure_s: int
    departure_utc: datetime


@dataclass(frozen=True)
class TripStop:
    stop_sequence: int
    stop_id: str
    arrival_s: int
    departure_s: int
    stop_name: str
    stop_lat: float
    stop_lon: float
    parent_station: str | None


# ─────────── Pure helpers ───────────


def parse_gtfs_time(text: str) -> int:
    """``"25:30:00"`` → seconds after the service-day anchor (may exceed 86400)."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"bad GTFS time {text!r}")
    h, m, s = (int(p) for p in parts)
    if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
        raise ValueError(f"bad GTFS time {text!r}")
    return h * 3600 + m * 60 + s


def service_instant(service_date: date, offset_s: int) -> datetime:
    """Absolute UTC instant of ``offset_s`` on GTFS service day ``service_date``.

    GTFS times count from "noon minus 12h" local time.  The subtraction is done
    in UTC: aware-datetime arithmetic in Python is wall-clock, which would be
    wrong on DST-change days.
    """
    noon = datetime.combine(service_date, time(12), tzinfo=NYC_TZ).astimezone(UTC)
    return noon - timedelta(hours=12) + timedelta(seconds=offset_s)


def rt_trip_id(static_trip_id: str) -> str | None:
    """NYCT RT trip_id: the static trip_id suffix after the first ``_``."""
    _, sep, suffix = static_trip_id.partition("_")
    return suffix if sep and suffix else None


def _parse_ymd(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y%m%d").date()


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ─────────── Schema ───────────

_SCHEMA = """
CREATE TABLE gtfs_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE gtfs_feed (
    feed_kind TEXT NOT NULL,
    feed_version TEXT NOT NULL,
    feed_sha256 TEXT NOT NULL,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE TABLE gtfs_stop (
    stop_id TEXT PRIMARY KEY,
    stop_name TEXT NOT NULL,
    stop_lat REAL NOT NULL,
    stop_lon REAL NOT NULL,
    parent_station TEXT,
    location_type INTEGER NOT NULL
);
CREATE TABLE gtfs_trip (
    trip_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    direction_id INTEGER,
    shape_id TEXT,
    rt_trip_id TEXT
);
CREATE TABLE gtfs_stop_time (
    trip_id TEXT NOT NULL REFERENCES gtfs_trip(trip_id),
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_s INTEGER NOT NULL,
    departure_s INTEGER NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX gtfs_stop_time_by_stop ON gtfs_stop_time (stop_id, departure_s);
CREATE TABLE gtfs_service (
    service_id TEXT PRIMARY KEY,
    monday INTEGER NOT NULL, tuesday INTEGER NOT NULL, wednesday INTEGER NOT NULL,
    thursday INTEGER NOT NULL, friday INTEGER NOT NULL, saturday INTEGER NOT NULL,
    sunday INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL
);
CREATE TABLE gtfs_service_date (
    service_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (service_id, date)
);
-- Every trip in the feed (not only allowlisted ones): RT suffix collisions can
-- involve a trip on any route.
CREATE TABLE gtfs_rt_trip (
    rt_trip_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    trip_id TEXT NOT NULL
);
CREATE INDEX gtfs_rt_trip_by_id ON gtfs_rt_trip (rt_trip_id);
"""


# ─────────── Import ───────────


@dataclass(frozen=True)
class _Trip:
    route_id: str
    service_id: str
    direction_id: int | None
    shape_id: str | None


def _open_csv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        text: IO[str] = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            if any(value is None for value in row.values()):
                raise FeedError(f"{name} row ending at line {reader.line_num} is short")
            yield row


def _open_rows(zf: zipfile.ZipFile, name: str, columns: Sequence[str]) -> Iterator[list[str]]:
    """Fast positional reader for the big files; yields rows ordered as ``columns``."""
    with zf.open(name) as raw:
        text: IO[str] = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        header = next(reader, None)
        if header is None:
            raise FeedError(f"{name} is empty")
        try:
            idx = [header.index(c) for c in columns]
        except ValueError as exc:
            raise FeedError(f"{name} lacks a required column: {exc}") from exc
        width = max(idx) + 1
        for row in reader:
            if len(row) < width:
                raise FeedError(
                    f"{name} row ending at line {reader.line_num} is short: "
                    f"expected at least {width} fields, got {len(row)}"
                )
            yield [row[i] for i in idx]


def _opt_int(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text else None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_stops(zf: zipfile.ZipFile) -> dict[str, StopInfo]:
    stops: dict[str, StopInfo] = {}
    for row in _open_csv(zf, "stops.txt"):
        sid = (row.get("stop_id") or "").strip()
        if not sid:
            continue
        try:
            lat = float(row.get("stop_lat") or "")
            lon = float(row.get("stop_lon") or "")
        except ValueError:
            continue
        stops[sid] = StopInfo(
            stop_id=sid,
            stop_name=(row.get("stop_name") or "").strip(),
            stop_lat=lat,
            stop_lon=lon,
            parent_station=(row.get("parent_station") or "").strip() or None,
            location_type=_opt_int(row.get("location_type")) or 0,
        )
    return stops


def _check_stop(
    stops: dict[str, StopInfo],
    stop_id: str,
    entry: AllowlistEntry,
    *,
    parent: str | None,
    location_type: int,
) -> str | None:
    stop = stops.get(stop_id)
    if stop is None:
        return f"{stop_id}: missing from stops.txt"
    if stop.location_type != location_type:
        return f"{stop_id}: location_type {stop.location_type} != {location_type}"
    if stop.parent_station != parent:
        return f"{stop_id}: parent_station {stop.parent_station!r} != {parent!r}"
    if normalize_name(stop.stop_name) != normalize_name(entry.stop_name):
        return f"{stop_id}: stop_name {stop.stop_name!r} != {entry.stop_name!r}"
    dist = haversine_m(stop.stop_lat, stop.stop_lon, entry.stop_lat, entry.stop_lon)
    if dist > entry.max_coord_distance_m:
        return f"{stop_id}: coordinates {dist:.0f} m from allowlist (> {entry.max_coord_distance_m:.0f} m)"
    return None


def validate_allowlist_against_stops(
    allowlist: Iterable[AllowlistEntry],
    stops: dict[str, StopInfo],
    route_ids: Iterable[str],
) -> None:
    """Raise ``AllowlistMismatchError`` listing every failed entry check."""
    known_routes = set(route_ids)
    problems: list[str] = []
    for entry in allowlist:
        problem = _check_stop(
            stops, entry.parent_station, entry, parent=None, location_type=1
        )
        if problem:
            problems.append(problem)
        for pid in entry.platform_ids:
            problem = _check_stop(stops, pid, entry, parent=entry.parent_station, location_type=0)
            if problem:
                problems.append(problem)
        for rid in entry.route_ids:
            if rid not in known_routes:
                problems.append(f"{entry.parent_station}: route_id {rid!r} not in routes.txt")
    if problems:
        raise AllowlistMismatchError("allowlist does not match stops.txt: " + "; ".join(problems))

def _read_feed_info(zf: zipfile.ZipFile) -> str | None:
    if "feed_info.txt" not in zf.namelist():
        return None
    for row in _open_csv(zf, "feed_info.txt"):
        version = (row.get("feed_version") or "").strip()
        return version or None
    return None


def _read_services(
    zf: zipfile.ZipFile,
) -> tuple[list[tuple[object, ...]], list[tuple[str, str, int]]]:
    names = set(zf.namelist())
    if "calendar.txt" not in names and "calendar_dates.txt" not in names:
        raise FeedError("feed has neither calendar.txt nor calendar_dates.txt")
    calendar: list[tuple[object, ...]] = []
    if "calendar.txt" in names:
        for row in _open_csv(zf, "calendar.txt"):
            try:
                flags = [int(row[d]) for d in _WEEKDAYS]
                start = _ymd(_parse_ymd(row["start_date"]))
                end = _ymd(_parse_ymd(row["end_date"]))
            except (KeyError, ValueError) as exc:
                raise FeedError(f"bad calendar.txt row {row!r}: {exc}") from exc
            calendar.append((row["service_id"].strip(), *flags, start, end))
    dates: list[tuple[str, str, int]] = []
    if "calendar_dates.txt" in names:
        for row in _open_csv(zf, "calendar_dates.txt"):
            try:
                kind = int(row["exception_type"])
                when = _ymd(_parse_ymd(row["date"]))
            except (KeyError, ValueError) as exc:
                raise FeedError(f"bad calendar_dates.txt row {row!r}: {exc}") from exc
            if kind not in (1, 2):
                raise FeedError(f"bad calendar_dates.txt exception_type {kind}")
            dates.append((row["service_id"].strip(), when, kind))
    return calendar, dates


def _coverage(
    calendar: list[tuple[object, ...]], dates: list[tuple[str, str, int]]
) -> tuple[date, date]:
    """Coverage = span of the service data.

    ``feed_info.txt`` is not used: the supplemented feed copies the base pick's
    ``feed_start_date``, which excludes the supplement days it actually adds
    (2026-10-31 had ``L0S8`` added while feed_start_date was 20261101).
    """
    bounds: list[str] = []
    for row in calendar:
        bounds.extend((str(row[8]), str(row[9])))
    bounds.extend(when for _, when, kind in dates if kind == 1)
    if not bounds:
        raise FeedError("feed defines no service dates")
    return _parse_ymd(min(bounds)), _parse_ymd(max(bounds))


def import_feed(
    zip_path: Path,
    db_path: Path,
    *,
    feed_kind: FeedKind,
    allowlist: Sequence[AllowlistEntry] | None = None,
    source_url: str = "",
    now: datetime | None = None,
) -> ImportStats:
    """Filtered import of ``zip_path`` into ``db_path`` (atomic replace)."""
    entries = tuple(allowlist) if allowlist is not None else load_allowlist()
    imported_at = (now or datetime.now(UTC)).astimezone(UTC)
    sha256 = _sha256_file(zip_path)

    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FeedError(f"not a readable zip: {exc}") from exc

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{db_path.name}.", suffix=".tmp", dir=db_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zf:
            missing = [n for n in _REQUIRED_FILES if n not in zf.namelist()]
            if missing:
                raise FeedError(f"feed lacks {missing}")
            stats = _build(
                zf, tmp_path, entries,
                feed_kind=feed_kind, sha256=sha256, source_url=source_url,
                imported_at=imported_at,
            )
        os.replace(tmp_path, db_path)
    except (zipfile.BadZipFile, UnicodeDecodeError, csv.Error, EOFError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise FeedError(f"corrupt feed: {exc}") from exc
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _logger.info(
        "gtfs import: %s feed %s (%s..%s), %d trips, %d stop_times, %d rejected",
        stats.feed_kind, stats.feed_version, stats.coverage_start, stats.coverage_end,
        stats.trips, stats.stop_times, stats.rejected_trips,
    )
    return stats


def _build(
    zf: zipfile.ZipFile,
    tmp_path: Path,
    entries: Sequence[AllowlistEntry],
    *,
    feed_kind: FeedKind,
    sha256: str,
    source_url: str,
    imported_at: datetime,
) -> ImportStats:
    stops = _read_stops(zf)
    route_ids = {
        (row.get("route_id") or "").strip() for row in _open_csv(zf, "routes.txt")
    }
    validate_allowlist_against_stops(entries, stops, route_ids)
    feed_version = _read_feed_info(zf) or sha256[:16]
    calendar, service_dates = _read_services(zf)
    coverage_start, coverage_end = _coverage(calendar, service_dates)

    # platform stop_id -> routes allowlisted at that platform
    platform_routes: dict[str, frozenset[str]] = {}
    for entry in entries:
        for pid in entry.platform_ids:
            platform_routes[pid] = platform_routes.get(pid, frozenset()) | set(entry.route_ids)

    trips: dict[str, _Trip] = {}
    rt_rows: list[tuple[str, str, str]] = []
    for trip_id, route_id, service_id, direction, shape in _open_rows(
        zf, "trips.txt", ("trip_id", "route_id", "service_id", "direction_id", "shape_id")
    ):
        try:
            direction_id = _opt_int(direction)
        except ValueError:
            direction_id = None
        trips[trip_id] = _Trip(route_id, service_id, direction_id, shape.strip() or None)
        rt = rt_trip_id(trip_id)
        if rt is not None:
            rt_rows.append((rt, service_id, trip_id))

    st_cols = ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time")
    # Pass 1: which trips stop at an allowlisted platform on an allowlisted route.
    matched: set[str] = set()
    for trip_id, stop_id, *_ in _open_rows(zf, "stop_times.txt", st_cols):
        allowed = platform_routes.get(stop_id)
        if allowed is None or trip_id in matched:
            continue
        trip = trips.get(trip_id)
        if trip is not None and trip.route_id in allowed:
            matched.add(trip_id)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)
        # Pass 2: full stop sequences of matched trips.
        rejected: set[str] = set()
        seen_stops: dict[str, set[str]] = {}
        seen_seq: dict[str, set[int]] = {}
        batch: list[tuple[str, int, str, int, int]] = []
        for trip_id, stop_id, seq_text, arr_text, dep_text in _open_rows(
            zf, "stop_times.txt", st_cols
        ):
            if trip_id not in matched or trip_id in rejected:
                continue
            try:
                seq = int(seq_text)
                arr_text, dep_text = arr_text.strip(), dep_text.strip()
                arr = parse_gtfs_time(arr_text or dep_text)
                dep = parse_gtfs_time(dep_text or arr_text)
            except ValueError:
                rejected.add(trip_id)
                continue
            stops_seen = seen_stops.setdefault(trip_id, set())
            seqs_seen = seen_seq.setdefault(trip_id, set())
            if stop_id in stops_seen or seq in seqs_seen:
                # A revisit would make (trip, stop) ambiguous for the bridge.
                rejected.add(trip_id)
                continue
            stops_seen.add(stop_id)
            seqs_seen.add(seq)
            batch.append((trip_id, seq, stop_id, arr, dep))
            if len(batch) >= 50_000:
                conn.executemany("INSERT INTO gtfs_stop_time VALUES (?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            conn.executemany("INSERT INTO gtfs_stop_time VALUES (?,?,?,?,?)", batch)
        if rejected:
            # A valid prefix may still rival another boarding. Removing these
            # trips would make an incomplete candidate inventory look unique.
            raise FeedError(
                f"incomplete matched trip inventory: {len(rejected)} trips have "
                "repeated stops/sequences or bad times"
            )
        kept = matched
        if not kept:
            raise FeedError("no allowlisted trips in feed")
        conn.executemany(
            "INSERT INTO gtfs_trip VALUES (?,?,?,?,?,?)",
            [
                (t, trips[t].route_id, trips[t].service_id, trips[t].direction_id,
                 trips[t].shape_id, rt_trip_id(t))
                for t in sorted(kept)
            ],
        )
        conn.executemany("INSERT INTO gtfs_rt_trip VALUES (?,?,?)", rt_rows)
        conn.executemany(
            "INSERT INTO gtfs_stop VALUES (?,?,?,?,?,?)",
            [
                (s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, s.parent_station,
                 s.location_type)
                for s in stops.values()
            ],
        )
        conn.executemany("INSERT INTO gtfs_service VALUES (?,?,?,?,?,?,?,?,?,?)", calendar)
        conn.executemany("INSERT OR REPLACE INTO gtfs_service_date VALUES (?,?,?)", service_dates)
        conn.execute(
            "INSERT INTO gtfs_feed VALUES (?,?,?,?,?,?,?)",
            (feed_kind, feed_version, sha256, coverage_start.isoformat(),
             coverage_end.isoformat(), imported_at.isoformat(), source_url),
        )
        conn.execute(
            "INSERT INTO gtfs_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        stop_time_count = conn.execute("SELECT COUNT(*) FROM gtfs_stop_time").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    with tmp_path.open("rb") as fh:
        os.fsync(fh.fileno())

    return ImportStats(
        feed_kind=feed_kind,
        feed_version=feed_version,
        feed_sha256=sha256,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        imported_at=imported_at,
        trips=len(kept),
        stop_times=int(stop_time_count),
        rejected_trips=len(rejected),
    )

# ─────────── Download / refresh ───────────

Fetcher = Callable[[str, Path], None]


def download(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` (bounded size); raises ``FeedError``."""
    import httpx

    partial: Path | None = None
    try:
        with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as resp:
            resp.raise_for_status()
            total = 0
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{dest.name}.", suffix=".part", dir=dest.parent, delete=False,
            ) as fh:
                partial = Path(fh.name)
                for chunk in resp.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise FeedError(f"{url}: download exceeds {_MAX_DOWNLOAD_BYTES} bytes")
                    fh.write(chunk)
            partial.replace(dest)
    except (httpx.HTTPError, OSError) as exc:
        raise FeedError(f"{url}: download failed: {exc}") from exc
    finally:
        if partial is not None:
            try:
                partial.unlink(missing_ok=True)
            except OSError as exc:
                _logger.warning("gtfs download cleanup failed: %s", type(exc).__name__)


def refresh(
    db_path: Path,
    *,
    allowlist: Sequence[AllowlistEntry] | None = None,
    fetch: Fetcher | None = None,
    tmp_dir: Path | None = None,
    now: datetime | None = None,
    feeds: Sequence[tuple[FeedKind, str]] = FEED_URLS,
) -> ImportStats:
    """Download and import the supplemented feed, falling back to regular (D5).

    The downloaded zip lives in a temp dir (default: next to ``db_path``) and
    is deleted afterwards.  Raises ``GtfsRefreshError`` when every feed fails;
    ``config_error`` is set when any attempt failed the allowlist check, since
    that needs a reviewed allowlist change rather than a retry.
    """
    entries = tuple(allowlist) if allowlist is not None else load_allowlist()
    fetcher = fetch or download
    work_dir = tmp_dir or db_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    allowlist_failed = False
    for kind, url in feeds:
        with tempfile.TemporaryDirectory(prefix=".gtfs-dl-", dir=work_dir) as td:
            zip_path = Path(td) / "feed.zip"
            try:
                fetcher(url, zip_path)
                return import_feed(
                    zip_path, db_path, feed_kind=kind, allowlist=entries,
                    source_url=url, now=now,
                )
            except Exception as exc:  # noqa: BLE001 - each feed attempt must fail open
                if isinstance(exc, AllowlistMismatchError):
                    allowlist_failed = True
                errors.append(f"{kind}: {type(exc).__name__}")
                _logger.warning("gtfs refresh: %s feed failed: %s", kind, type(exc).__name__)
    raise GtfsRefreshError(
        "gtfs refresh failed; previous index kept: " + " | ".join(errors),
        config_error=allowlist_failed,
    )


# ─────────── Read API ───────────


class GtfsIndex:
    """Read-only view over an imported ``gtfs.db``.  Use :meth:`open`."""

    def __init__(self, conn: sqlite3.Connection, info: FeedInfo) -> None:
        self._conn = conn
        self._info = info

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        now: datetime | None = None,
        max_age: timedelta = MAX_INDEX_AGE,
    ) -> GtfsIndex | None:
        """Open ``path`` or return ``None`` if missing, corrupt or stale."""
        p = Path(path)
        if not p.is_file():
            return None
        current = (now or datetime.now(UTC)).astimezone(UTC)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            meta = conn.execute(
                "SELECT value FROM gtfs_meta WHERE key = 'schema_version'"
            ).fetchone()
            if meta is None or meta[0] != str(SCHEMA_VERSION):
                raise ValueError("unknown schema version")
            rows = conn.execute(
                "SELECT feed_kind, feed_version, feed_sha256, coverage_start, coverage_end,"
                " imported_at, source_url FROM gtfs_feed"
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("expected exactly one gtfs_feed row")
            kind, version, sha, start, end, imported, url = rows[0]
            if kind not in ("supplemented", "regular"):
                raise ValueError(f"unknown feed_kind {kind!r}")
            imported_at = datetime.fromisoformat(imported)
            if imported_at.tzinfo is None:
                raise ValueError("naive imported_at")
            info = FeedInfo(
                feed_kind=kind,
                feed_version=version,
                feed_sha256=sha,
                coverage_start=date.fromisoformat(start),
                coverage_end=date.fromisoformat(end),
                imported_at=imported_at.astimezone(UTC),
                source_url=url,
            )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            _logger.warning("gtfs index %s unusable: %s", p, exc)
            if conn is not None:
                conn.close()
            return None

        age = current - info.imported_at
        today = current.astimezone(NYC_TZ).date()
        if age > max_age or age < -_FUTURE_SKEW:
            _logger.info("gtfs index %s stale (imported %s)", p, info.imported_at)
        elif not info.coverage_start <= today <= info.coverage_end:
            _logger.info("gtfs index %s does not cover %s", p, today)
        else:
            return cls(conn, info)
        conn.close()
        return None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def feed_info(self) -> FeedInfo:
        return self._info

    def covers(self, service_date: date) -> bool:
        return self._info.coverage_start <= service_date <= self._info.coverage_end

    def service_active(self, service_id: str, service_date: date) -> bool:
        """``calendar`` weekday + range, overridden by ``calendar_dates``."""
        if not self.covers(service_date):
            return False
        ymd = _ymd(service_date)
        exc = self._conn.execute(
            "SELECT exception_type FROM gtfs_service_date WHERE service_id = ? AND date = ?",
            (service_id, ymd),
        ).fetchone()
        if exc is not None:
            return int(exc[0]) == 1
        row = self._conn.execute(
            f"SELECT {_WEEKDAYS[service_date.weekday()]}, start_date, end_date"
            " FROM gtfs_service WHERE service_id = ?",
            (service_id,),
        ).fetchone()
        if row is None:
            return False
        flag, start, end = row
        return bool(flag) and str(start) <= ymd <= str(end)

    def departures_near(
        self,
        platform_stop_ids: Iterable[str],
        route_ids: Iterable[str],
        instant_utc: datetime,
        window_s: int = 30,
    ) -> list[Departure]:
        """Stop events within ``window_s`` of ``instant_utc`` (see module doc)."""
        if instant_utc.tzinfo is None:
            raise ValueError("instant_utc must be timezone-aware")
        if window_s < 0:
            raise ValueError("window_s must be >= 0")
        platforms = sorted(set(platform_stop_ids))
        routes = sorted(set(route_ids))
        if not platforms or not routes:
            return []
        target = instant_utc.astimezone(UTC)
        lo, hi = target - timedelta(seconds=window_s), target + timedelta(seconds=window_s)
        out: list[Departure] = []
        p_marks = ",".join("?" * len(platforms))
        r_marks = ",".join("?" * len(routes))
        sql = (
            "SELECT t.trip_id, t.rt_trip_id, t.route_id, t.service_id, t.direction_id,"
            " t.shape_id, st.stop_id, st.stop_sequence, st.arrival_s, st.departure_s"
            " FROM gtfs_stop_time st JOIN gtfs_trip t ON t.trip_id = st.trip_id"
            f" WHERE st.stop_id IN ({p_marks}) AND t.route_id IN ({r_marks})"
            " AND st.departure_s BETWEEN ? AND ?"
        )
        # Bound possible anchors using actual offsets, not a fixed number of
        # local days. GTFS offsets can exceed 48h, and spring-forward's anchor
        # can lie on the previous local date. Adding 12h recovers local noon
        # (and hence the service date), even across an offset transition.
        bounds = self._conn.execute(
            "SELECT MIN(st.departure_s), MAX(st.departure_s)"
            " FROM gtfs_stop_time st JOIN gtfs_trip t ON t.trip_id = st.trip_id"
            f" WHERE st.stop_id IN ({p_marks}) AND t.route_id IN ({r_marks})",
            [*platforms, *routes],
        ).fetchone()
        if bounds is None or bounds[0] is None:
            return []
        first_noon = lo - timedelta(seconds=int(bounds[1])) + timedelta(hours=12)
        last_noon = hi - timedelta(seconds=int(bounds[0])) + timedelta(hours=12)
        first = max(first_noon.astimezone(NYC_TZ).date(), self._info.coverage_start)
        last = min(last_noon.astimezone(NYC_TZ).date(), self._info.coverage_end)
        active: dict[tuple[str, date], bool] = {}
        for i in range((last - first).days + 1):
            service_date = first + timedelta(days=i)
            anchor = service_instant(service_date, 0)
            lo_s = int((lo - anchor).total_seconds())
            hi_s = int((hi - anchor).total_seconds()) + 1
            if hi_s < 0:
                continue
            for row in self._conn.execute(sql, (*platforms, *routes, lo_s, hi_s)):
                trip_id, rt, route_id, service_id, direction, shape, stop_id, seq, arr, dep = row
                when = service_instant(service_date, int(dep))
                if abs((when - target).total_seconds()) > window_s:
                    continue
                key = (service_id, service_date)
                if key not in active:
                    active[key] = self.service_active(service_id, service_date)
                if not active[key]:
                    continue
                out.append(
                    Departure(
                        trip_id=trip_id, rt_trip_id=rt, route_id=route_id,
                        service_id=service_id, direction_id=direction, shape_id=shape,
                        stop_id=stop_id, stop_sequence=int(seq), service_date=service_date,
                        arrival_s=int(arr), departure_s=int(dep), departure_utc=when,
                    )
                )
        out.sort(key=lambda d: (d.departure_utc, d.trip_id, d.stop_sequence))
        return out

    def trip_stops(self, trip_id: str) -> list[TripStop]:
        rows = self._conn.execute(
            "SELECT st.stop_sequence, st.stop_id, st.arrival_s, st.departure_s,"
            " s.stop_name, s.stop_lat, s.stop_lon, s.parent_station"
            " FROM gtfs_stop_time st JOIN gtfs_stop s ON s.stop_id = st.stop_id"
            " WHERE st.trip_id = ? ORDER BY st.stop_sequence",
            (trip_id,),
        ).fetchall()
        return [
            TripStop(
                stop_sequence=int(r[0]), stop_id=r[1], arrival_s=int(r[2]),
                departure_s=int(r[3]), stop_name=r[4], stop_lat=float(r[5]),
                stop_lon=float(r[6]), parent_station=r[7],
            )
            for r in rows
        ]

    def stop(self, stop_id: str) -> StopInfo | None:
        r = self._conn.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon, parent_station, location_type"
            " FROM gtfs_stop WHERE stop_id = ?",
            (stop_id,),
        ).fetchone()
        if r is None:
            return None
        return StopInfo(r[0], r[1], float(r[2]), float(r[3]), r[4], int(r[5]))

    def rt_trip_id_unique(self, rt_trip_id: str, service_date: date) -> bool:
        """True iff exactly one trip active on ``service_date`` maps to ``rt_trip_id``."""
        if not self.covers(service_date):
            return False
        rows = self._conn.execute(
            "SELECT trip_id, service_id FROM gtfs_rt_trip WHERE rt_trip_id = ?",
            (rt_trip_id,),
        ).fetchall()
        active = [t for t, s in rows if self.service_active(s, service_date)]
        return len(active) == 1
