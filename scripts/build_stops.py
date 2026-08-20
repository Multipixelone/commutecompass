#!/usr/bin/env python3
"""Generate the bundled GTFS stop tables used by :mod:`commutecompass.realtime`.

The real-time departure buffer fuzzy-matches a Google Directions boarding-stop
*name* to a GTFS ``stop_id`` so it can look that stop up in the matching
GTFS-RT trip-update feed.  That mapping comes from the MTA's **static** GTFS
``stops.txt``.  Rather than depend on a multi-megabyte feed at runtime, we
distill each system's ``stops.txt`` into a compact CSV checked into the package
(``src/commutecompass/data/stops_{subway,lirr,bus}.csv``).

Run this to refresh the bundled data when the MTA reorganizes stops:

    python scripts/build_stops.py

It downloads the current static GTFS zips, extracts ``stops.txt``, and writes
the three CSVs.  Network access is required (only when refreshing).

CSV schema (subway, lirr): ``stop_id,stop_name,parent_station``
CSV schema (bus):          ``stop_id,stop_name``

Subway stop ids are directional at the platform level (``R20N``/``R20S``) with a
parent station (``R20``).  We keep the parent stations (so a name matches once)
plus their ``parent_station`` linkage; ``realtime`` expands a parent to its
``…N``/``…S`` children when querying the feed.  Bus stops are already
directional, so no parent column is needed.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path
from typing import Iterable

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "commutecompass" / "data"

# Static GTFS bundles (verify against https://www.mta.info/developers).
SUBWAY_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip"
LIRR_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip"
# NYC bus static GTFS is split per operator; merge them all.
BUS_GTFS_URLS = [
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_b.zip",  # Brooklyn
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_bx.zip",  # Bronx
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_m.zip",  # Manhattan
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_q.zip",  # Queens
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_si.zip",  # Staten Island
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_busco.zip",  # MTA Bus Company
]


def _read_stops(zip_url: str) -> list[dict[str, str]]:
    """Download a GTFS zip and return its ``stops.txt`` rows as dicts."""
    print(f"  fetching {zip_url}", file=sys.stderr)
    resp = httpx.get(zip_url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open("stops.txt") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig")
            return list(csv.DictReader(text))


def _write_csv(path: Path, rows: Iterable[tuple[str, ...]], header: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            key = row[0]
            if key in seen:
                continue
            seen.add(key)
            writer.writerow(row)
            count += 1
    print(f"  wrote {count} rows -> {path}", file=sys.stderr)


def build_rail(zip_url: str, out_name: str) -> None:
    """Subway/LIRR: keep parent stations (and their parent linkage)."""
    rows = _read_stops(zip_url)
    out: list[tuple[str, str, str]] = []
    for r in rows:
        stop_id = (r.get("stop_id") or "").strip()
        name = (r.get("stop_name") or "").strip()
        parent = (r.get("parent_station") or "").strip()
        if not stop_id or not name:
            continue
        # Prefer parent stations (location_type==1) and any stop without a parent
        # — those are the named stations a rider boards at.  Directional children
        # are reconstructed at query time.
        location_type = (r.get("location_type") or "").strip()
        if location_type == "1" or not parent:
            out.append((stop_id, name, parent))
    _write_csv(DATA_DIR / out_name, out, ("stop_id", "stop_name", "parent_station"))


def build_bus(out_name: str) -> None:
    rows: list[tuple[str, str]] = []
    for url in BUS_GTFS_URLS:
        for r in _read_stops(url):
            stop_id = (r.get("stop_id") or "").strip()
            name = (r.get("stop_name") or "").strip()
            if stop_id and name:
                rows.append((stop_id, name))
    _write_csv(DATA_DIR / out_name, rows, ("stop_id", "stop_name"))


def main() -> int:
    print("building subway stops...", file=sys.stderr)
    build_rail(SUBWAY_GTFS_URL, "stops_subway.csv")
    print("building LIRR stops...", file=sys.stderr)
    build_rail(LIRR_GTFS_URL, "stops_lirr.csv")
    print("building bus stops...", file=sys.stderr)
    build_bus("stops_bus.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
