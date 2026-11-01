"""Hermetic tests for the static GTFS importer and SQLite index."""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from commutecompass.gtfs_allowlist import AllowlistEntry
from commutecompass.gtfs_index import (
    AllowlistMismatchError,
    FeedError,
    GtfsIndex,
    GtfsRefreshError,
    import_feed,
    parse_gtfs_time,
    refresh,
    rt_trip_id,
    service_instant,
)
from commutecompass.timeutil import NYC_TZ

ENTRY = AllowlistEntry(
    route_ids=("A", "C"),
    directions_line=("A", "C"),
    parent_station="A48",
    stop_name="Utica Av",
    stop_lat=40.679364,
    stop_lon=-73.930729,
    platforms={"N": "A48N", "S": "A48S"},
    directions_names=("Utica Av",),
    max_coord_distance_m=150,
)
PLATFORMS = ("A48N", "A48S")
ROUTES = ("A", "C")

STOPS = """stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station
A48,Utica Av,40.679364,-73.930729,1,
A48N,Utica Av,40.679364,-73.930729,,A48
A48S,Utica Av,40.679364,-73.930729,,A48
A47,Kingston-Throop Avs,40.679921,-73.940858,1,
A47N,Kingston-Throop Avs,40.679921,-73.940858,,A47
A47S,Kingston-Throop Avs,40.679921,-73.940858,,A47
A49,Ralph Av,40.678822,-73.920786,1,
A49N,Ralph Av,40.678822,-73.920786,,A49
A49S,Ralph Av,40.678822,-73.920786,,A49
"""

ROUTES_TXT = """route_id,route_short_name,route_type
A,A,1
C,C,1
E,E,1
"""

CALENDAR = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
Weekday,1,1,1,1,1,0,0,20261024,20270331
Saturday,0,0,0,0,0,1,0,20261024,20270331
Sunday,0,0,0,0,0,0,1,20261024,20270331
"""

CALENDAR_DATES = """service_id,date,exception_type
L0S8,20261031,1
L1S7,20261031,1
L1S7,20261107,1
Weekday,20261126,2
Sunday,20261126,1
"""

# trip_id, route, service, direction, shape, [(stop, time)]
TRIPS: list[tuple[str, str, str, int, str, list[tuple[str, str]]]] = [
    ("WK_049300_A..N", "A", "Weekday", 0, "A..N",
     [("A49N", "08:28:30"), ("A48N", "08:30:00"), ("A47N", "08:32:00")]),
    # Friday-night trip running past 24:00.
    ("WK_147000_A..N", "A", "Weekday", 0, "A..N",
     [("A49N", "24:28:30"), ("A48N", "24:30:00"), ("A47N", "24:32:00")]),
    # DST fall-back: Saturday 25:30 and Sunday 01:30 are distinct instants.
    ("SA_152700_C..S", "C", "Saturday", 1, "C..S",
     [("A47S", "25:28:00"), ("A48S", "25:30:00"), ("A49S", "25:32:00")]),
    ("SU_008700_C..S", "C", "Sunday", 1, "C..S",
     [("A47S", "01:28:00"), ("A48S", "01:30:00"), ("A49S", "01:32:00")]),
    # Spring-forward: nominal 02:30 sits in the DST gap.
    ("SU_014700_A..S", "A", "Sunday", 1, "A..S",
     [("A47S", "02:28:00"), ("A48S", "02:30:00"), ("A49S", "02:32:00")]),
    ("SU_048000_A..S", "A", "Sunday", 1, "A..S",
     [("A47S", "07:58:00"), ("A48S", "08:00:00"), ("A49S", "08:02:00")]),
    # calendar_dates-only pick services sharing one RT suffix on 20261031.
    ("L0S8-A_060000_A..N", "A", "L0S8", 0, "A..N",
     [("A49N", "09:58:00"), ("A48N", "10:00:00"), ("A47N", "10:02:00")]),
    ("L1S7-A_060000_A..N", "A", "L1S7", 0, "A..N",
     [("A49N", "09:58:00"), ("A47N", "10:02:00")]),
    # Revisits A48N → rejected.
    ("WK_060000_C..N", "C", "Weekday", 0, "C..N",
     [("A48N", "10:00:00"), ("A47N", "10:02:00"), ("A48N", "10:04:00")]),
    # Not an allowlisted route at A48 → not imported.
    ("WK_060000_E..N", "E", "Weekday", 0, "E..N",
     [("A48N", "10:00:00"), ("A47N", "10:02:00")]),
]


def _build_zip(path: Path, *, stops: str = STOPS, extra: Optional[dict[str, str]] = None) -> Path:
    trips = ["route_id,trip_id,service_id,trip_headsign,direction_id,shape_id"]
    stop_times = ["trip_id,stop_id,arrival_time,departure_time,stop_sequence"]
    for trip_id, route, service, direction, shape, seq in TRIPS:
        trips.append(f"{route},{trip_id},{service},X,{direction},{shape}")
        for i, (stop, t) in enumerate(seq, start=1):
            stop_times.append(f"{trip_id},{stop},{t},{t},{i}")
    files = {
        "feed_info.txt": "feed_publisher_name,feed_start_date,feed_end_date,feed_version\n"
        "MTA,20261101,20270331,fixture-v1\n",
        "stops.txt": stops,
        "routes.txt": ROUTES_TXT,
        "trips.txt": "\n".join(trips) + "\n",
        "stop_times.txt": "\n".join(stop_times) + "\n",
        "calendar.txt": CALENDAR,
        "calendar_dates.txt": CALENDAR_DATES,
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def _nyc(y: int, mo: int, d: int, h: int, mi: int, fold: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NYC_TZ, fold=fold)


def _open_at(tmp_path: Path, when: datetime) -> GtfsIndex:
    zip_path = _build_zip(tmp_path / "feed.zip")
    db = tmp_path / "gtfs.db"
    import_feed(zip_path, db, feed_kind="supplemented", allowlist=[ENTRY], now=when)
    index = GtfsIndex.open(db, now=when)
    assert index is not None
    return index


# ─────────── Pure helpers ───────────


def test_parse_gtfs_time() -> None:
    assert parse_gtfs_time("00:00:00") == 0
    assert parse_gtfs_time("25:30:00") == 25 * 3600 + 1800
    for bad in ("1:2", "08:61:00", "x:00:00"):
        with pytest.raises(ValueError):
            parse_gtfs_time(bad)


def test_rt_trip_id_suffix() -> None:
    assert rt_trip_id("L0S8-N-2058-S17_087600_N..S36R") == "087600_N..S36R"
    assert rt_trip_id("noprefix") is None


def test_service_instant_dst_anchor() -> None:
    # Ordinary day: 08:30 EST.
    assert service_instant(date(2026, 11, 2), 8 * 3600 + 1800) == datetime(
        2026, 11, 2, 13, 30, tzinfo=UTC
    )
    # Fall-back: Saturday 25:30 = first 01:30 (EDT); Sunday 01:30 = second (EST).
    assert service_instant(date(2026, 10, 31), 25 * 3600 + 1800) == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )
    assert service_instant(date(2026, 11, 1), 3600 + 1800) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )
    # Spring-forward: anchor is 23:00 EST the day before, so nominal 02:30 is
    # 06:30 UTC and 08:00 is still 08:00 EDT.
    assert service_instant(date(2027, 3, 14), 2 * 3600 + 1800) == datetime(
        2027, 3, 14, 6, 30, tzinfo=UTC
    )
    assert service_instant(date(2027, 3, 14), 8 * 3600) == datetime(
        2027, 3, 14, 12, 0, tzinfo=UTC
    )


# ─────────── Import ───────────


def test_import_filters_and_records_metadata(tmp_path: Path) -> None:
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    zip_path = _build_zip(tmp_path / "feed.zip")
    stats = import_feed(
        zip_path, tmp_path / "gtfs.db", feed_kind="regular", allowlist=[ENTRY], now=now,
        source_url="https://example/feed.zip",
    )
    assert stats.feed_kind == "regular"
    assert stats.feed_version == "fixture-v1"
    assert len(stats.feed_sha256) == 64
    # Coverage comes from service data (10-24 calendar start), not feed_info.
    assert stats.coverage_start == date(2026, 10, 24)
    assert stats.coverage_end == date(2027, 3, 31)
    assert stats.rejected_trips == 1
    assert stats.trips == 7  # 10 trips − E route − L1S7 (no A48) − revisit

    with GtfsIndex.open(tmp_path / "gtfs.db", now=now) as index:  # type: ignore[union-attr]
        info = index.feed_info()
        assert info.imported_at == now
        assert info.source_url == "https://example/feed.zip"
        assert info.feed_sha256 == stats.feed_sha256
        assert index.trip_stops("WK_060000_C..N") == []  # revisit rejected
        assert index.trip_stops("WK_060000_E..N") == []  # wrong route
        stops = index.trip_stops("WK_049300_A..N")
        assert [s.stop_id for s in stops] == ["A49N", "A48N", "A47N"]
        assert stops[1].parent_station == "A48"
        assert stops[2].stop_name == "Kingston-Throop Avs"
        stop = index.stop("A48")
        assert stop is not None and stop.location_type == 1


@pytest.mark.parametrize(
    "stops",
    [
        STOPS.replace("A48,Utica Av,", "A48,Utica Avenue,"),
        STOPS.replace("A48S,Utica Av,40.679364", "A48S,Utica Av,40.689364"),
        "\n".join(line for line in STOPS.splitlines() if not line.startswith("A48N")) + "\n",
        STOPS.replace("A48N,Utica Av,40.679364,-73.930729,,A48", "A48N,Utica Av,40.679364,-73.930729,,A47"),
    ],
    ids=["name", "coords", "missing-platform", "wrong-parent"],
)
def test_allowlist_mismatch_fails_and_keeps_previous(tmp_path: Path, stops: str) -> None:
    db = tmp_path / "gtfs.db"
    good = _build_zip(tmp_path / "good.zip")
    import_feed(good, db, feed_kind="supplemented", allowlist=[ENTRY])
    before = db.read_bytes()
    bad = _build_zip(tmp_path / "bad.zip", stops=stops)
    with pytest.raises(AllowlistMismatchError):
        import_feed(bad, db, feed_kind="supplemented", allowlist=[ENTRY])
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.zip", "good.zip", "gtfs.db"]


def test_unknown_route_in_allowlist_fails(tmp_path: Path) -> None:
    entry = AllowlistEntry(**{**ENTRY.__dict__, "route_ids": ("A", "Z")})
    with pytest.raises(AllowlistMismatchError, match="route_id"):
        import_feed(_build_zip(tmp_path / "f.zip"), tmp_path / "g.db", feed_kind="regular",
                    allowlist=[entry])


def test_corrupt_zip_fails_and_keeps_previous(tmp_path: Path) -> None:
    db = tmp_path / "gtfs.db"
    import_feed(_build_zip(tmp_path / "good.zip"), db, feed_kind="supplemented", allowlist=[ENTRY])
    before = db.read_bytes()
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(FeedError):
        import_feed(junk, db, feed_kind="supplemented", allowlist=[ENTRY])
    no_trips = _build_zip(tmp_path / "empty.zip", extra={
        "stop_times.txt": "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
    })
    with pytest.raises(FeedError, match="no allowlisted trips"):
        import_feed(no_trips, db, feed_kind="supplemented", allowlist=[ENTRY])
    assert db.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# ─────────── departures_near ───────────


def _trip_ids(index: GtfsIndex, when: datetime, window_s: int = 30) -> list[tuple[str, date]]:
    return [
        (d.trip_id, d.service_date)
        for d in index.departures_near(PLATFORMS, ROUTES, when, window_s=window_s)
    ]


def test_ordinary_weekday_and_window(tmp_path: Path) -> None:
    when = _nyc(2026, 11, 2, 8, 30)
    index = _open_at(tmp_path, when)
    (dep,) = index.departures_near(PLATFORMS, ROUTES, when)
    assert dep.trip_id == "WK_049300_A..N"
    assert dep.rt_trip_id == "049300_A..N"
    assert dep.stop_id == "A48N" and dep.stop_sequence == 2
    assert dep.direction_id == 0 and dep.shape_id == "A..N"
    assert dep.service_date == date(2026, 11, 2)
    assert dep.departure_utc == datetime(2026, 11, 2, 13, 30, tzinfo=UTC)
    assert _trip_ids(index, when + timedelta(seconds=30)) == [("WK_049300_A..N", date(2026, 11, 2))]
    assert _trip_ids(index, when + timedelta(seconds=31)) == []
    assert index.departures_near(PLATFORMS, ("E",), when) == []
    assert index.departures_near(("A47N",), ROUTES, when) == []
    with pytest.raises(ValueError):
        index.departures_near(PLATFORMS, ROUTES, datetime(2026, 11, 2, 8, 30))


def test_after_midnight_trip_uses_previous_service_day(tmp_path: Path) -> None:
    # Saturday 00:30 is Friday's 24:30 trip.
    when = _nyc(2026, 11, 7, 0, 30)
    index = _open_at(tmp_path, when)
    assert _trip_ids(index, when) == [("WK_147000_A..N", date(2026, 11, 6))]
    # Sunday 00:30: Saturday has no 24:30 trip.
    assert _trip_ids(index, _nyc(2026, 11, 8, 0, 30)) == []


def test_dst_fall_back_distinguishes_both_0130s(tmp_path: Path) -> None:
    first = _nyc(2026, 11, 1, 1, 30, fold=0)   # EDT, 05:30 UTC
    second = _nyc(2026, 11, 1, 1, 30, fold=1)  # EST, 06:30 UTC
    assert first.astimezone(UTC).hour == 5 and second.astimezone(UTC).hour == 6
    index = _open_at(tmp_path, first)
    assert _trip_ids(index, first) == [("SA_152700_C..S", date(2026, 10, 31))]
    assert _trip_ids(index, second) == [("SU_008700_C..S", date(2026, 11, 1))]


def test_dst_spring_forward_gap(tmp_path: Path) -> None:
    anchor_instant = datetime(2027, 3, 14, 6, 30, tzinfo=UTC)
    index = _open_at(tmp_path, anchor_instant)
    # Nominal 02:30 (anchor 23:00 EST + 3.5h) is 06:30 UTC — the same instant as
    # Saturday's 25:30 trip.  Both are returned; the bridge's "exactly one"
    # rule then fails closed.
    assert _trip_ids(index, anchor_instant) == [
        ("SA_152700_C..S", date(2027, 3, 13)),
        ("SU_014700_A..S", date(2027, 3, 14)),
    ]
    # A "wall-clock 02:30" reading (03:30 EDT, 07:30 UTC) must not match.
    assert _trip_ids(index, datetime(2027, 3, 14, 7, 30, tzinfo=UTC)) == []
    # Later trips that day are at their normal EDT wall time.
    assert _trip_ids(index, _nyc(2027, 3, 14, 8, 0)) == [("SU_048000_A..S", date(2027, 3, 14))]


def test_calendar_dates_add_and_remove(tmp_path: Path) -> None:
    sat = _nyc(2026, 10, 31, 10, 0)
    index = _open_at(tmp_path, sat)
    # L0S8 exists only via calendar_dates add on 20261031.
    assert _trip_ids(index, sat) == [("L0S8-A_060000_A..N", date(2026, 10, 31))]
    assert index.service_active("L0S8", date(2026, 10, 31))
    assert not index.service_active("L0S8", date(2026, 11, 7))
    # Weekday removed on Thanksgiving; Sunday added.
    thanksgiving = date(2026, 11, 26)
    assert not index.service_active("Weekday", thanksgiving)
    assert index.service_active("Sunday", thanksgiving)
    assert _trip_ids(index, _nyc(2026, 11, 26, 8, 30)) == []
    assert _trip_ids(index, _nyc(2026, 11, 25, 8, 30)) == [("WK_049300_A..N", date(2026, 11, 25))]
    assert not index.service_active("NoSuchService", thanksgiving)


def test_rt_trip_id_unique(tmp_path: Path) -> None:
    index = _open_at(tmp_path, _nyc(2026, 10, 31, 10, 0))
    # L0S8 and L1S7 both active on 10-31 with suffix 060000_A..N.
    assert not index.rt_trip_id_unique("060000_A..N", date(2026, 10, 31))
    # On 11-07 only L1S7 is active.
    assert index.rt_trip_id_unique("060000_A..N", date(2026, 11, 7))
    assert index.rt_trip_id_unique("049300_A..N", date(2026, 11, 2))
    assert not index.rt_trip_id_unique("049300_A..N", date(2026, 11, 1))  # Sunday
    assert not index.rt_trip_id_unique("nope", date(2026, 11, 2))
    # The colliding L1S7 trip never stops at A48 (not imported), yet it still
    # counts above; unimported trips are known to the suffix table.
    assert index.rt_trip_id_unique("060000_E..N", date(2026, 11, 2))
    assert index.trip_stops("L1S7-A_060000_A..N") == []
    assert not index.rt_trip_id_unique("049300_A..N", date(2028, 1, 3))  # outside coverage


# ─────────── open(): missing / corrupt / stale ───────────


def test_open_missing_and_corrupt(tmp_path: Path) -> None:
    assert GtfsIndex.open(tmp_path / "absent.db") is None
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"garbage" * 100)
    assert GtfsIndex.open(junk) is None
    empty = tmp_path / "empty.db"
    import sqlite3

    sqlite3.connect(empty).close()
    assert GtfsIndex.open(empty) is None


def test_open_stale_future_and_out_of_coverage(tmp_path: Path) -> None:
    imported = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    db = tmp_path / "gtfs.db"
    import_feed(_build_zip(tmp_path / "f.zip"), db, feed_kind="supplemented",
                allowlist=[ENTRY], now=imported)
    ok = GtfsIndex.open(db, now=imported + timedelta(days=8))
    assert ok is not None
    ok.close()
    assert GtfsIndex.open(db, now=imported + timedelta(days=8, seconds=1)) is None
    assert GtfsIndex.open(db, now=imported - timedelta(hours=1)) is None

    late = datetime(2027, 3, 31, 12, 0, tzinfo=UTC)
    import_feed(_build_zip(tmp_path / "f.zip"), db, feed_kind="supplemented",
                allowlist=[ENTRY], now=late)
    assert GtfsIndex.open(db, now=late) is not None
    # 2027-04-01 NYC is past coverage_end even though the import is fresh.
    assert GtfsIndex.open(db, now=datetime(2027, 4, 1, 12, 0, tzinfo=UTC)) is None


# ─────────── refresh(): supplemented → regular fallback ───────────


def test_refresh_prefers_supplemented(tmp_path: Path) -> None:
    src = _build_zip(tmp_path / "src.zip")
    urls: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        urls.append(url)
        dest.write_bytes(src.read_bytes())

    stats = refresh(tmp_path / "gtfs.db", allowlist=[ENTRY], fetch=fetch)
    assert stats.feed_kind == "supplemented"
    assert urls == ["https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip"]


def test_refresh_falls_back_to_regular(tmp_path: Path) -> None:
    src = _build_zip(tmp_path / "src.zip")

    def fetch(url: str, dest: Path) -> None:
        if "supplemented" in url:
            dest.write_bytes(b"truncated")  # validation failure, not download failure
        else:
            dest.write_bytes(src.read_bytes())

    stats = refresh(tmp_path / "gtfs.db", allowlist=[ENTRY], fetch=fetch)
    assert stats.feed_kind == "regular"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".gtfs-dl-")]


def test_refresh_both_fail_is_transient(tmp_path: Path) -> None:
    def fetch(url: str, dest: Path) -> None:
        raise FeedError(f"{url}: down")

    with pytest.raises(GtfsRefreshError) as excinfo:
        refresh(tmp_path / "gtfs.db", allowlist=[ENTRY], fetch=fetch)
    assert excinfo.value.config_error is False
    assert not (tmp_path / "gtfs.db").exists()


def test_refresh_allowlist_failure_is_config_error(tmp_path: Path) -> None:
    src = _build_zip(tmp_path / "src.zip", stops=STOPS.replace("A48,Utica Av,", "A48,Elsewhere,"))

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(src.read_bytes())

    with pytest.raises(GtfsRefreshError) as excinfo:
        refresh(tmp_path / "gtfs.db", allowlist=[ENTRY], fetch=fetch)
    assert excinfo.value.config_error is True
