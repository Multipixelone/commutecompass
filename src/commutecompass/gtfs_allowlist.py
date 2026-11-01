"""Vendored stage-1 allowlist for the GTFS identity bridge.

The allowlist (``data/gtfs_bridge_allowlist.toml``) is the reviewed support
set of the bridge (docs/realtime-identity-bridge.md §3).  Only legs boarding at
a listed platform on a listed route can ever receive trusted GTFS identity.

Public API:

- ``AllowlistEntry`` — one station entry (frozen dataclass).
- ``load_allowlist(path=None) -> tuple[AllowlistEntry, ...]`` — parse and
  strictly validate; raises ``AllowlistError`` on any schema problem.  With no
  ``path`` the bundled package resource is used.
- ``normalize_line(value)`` / ``normalize_name(value)`` — the exact-match
  normalizations used for ``directions_line`` / ``directions_names``
  (uppercase/whitespace for lines, whitespace + casefold for names).

Validation is strict: unknown keys, missing keys, wrong types, empty lists,
duplicate parents/platforms, platforms not derived from the parent, or
coordinates outside the NYC bounding box all raise.  The importer separately
checks every entry against the static ``stops.txt``.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

ALLOWLIST_RESOURCE = "gtfs_bridge_allowlist.toml"
SCHEMA_VERSION = 1

_ENTRY_KEYS = frozenset(
    {
        "route_ids",
        "directions_line",
        "parent_station",
        "stop_name",
        "stop_lat",
        "stop_lon",
        "platforms",
        "directions_names",
        "max_coord_distance_m",
    }
)
_TOP_KEYS = frozenset({"schema_version", "platform"})
_DIRECTIONS = ("N", "S")
# Generous NYC bounding box: catches swapped lat/lon and sign errors.
_LAT_RANGE = (40.4, 41.1)
_LON_RANGE = (-74.3, -73.6)
_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
_WS_RE = re.compile(r"\s+")


class AllowlistError(ValueError):
    """The allowlist file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class AllowlistEntry:
    route_ids: tuple[str, ...]
    directions_line: tuple[str, ...]
    parent_station: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    platforms: dict[str, str]  # {"N": "A48N", "S": "A48S"}
    directions_names: tuple[str, ...]
    max_coord_distance_m: float

    @property
    def platform_ids(self) -> tuple[str, ...]:
        return tuple(self.platforms[d] for d in _DIRECTIONS)


def normalize_line(value: str) -> str:
    """Uppercase and drop all whitespace (``" 6x "`` → ``"6X"``)."""
    return _WS_RE.sub("", value).upper()


def normalize_name(value: str) -> str:
    """Collapse whitespace and casefold; punctuation is kept (exact match)."""
    return _WS_RE.sub(" ", value).strip().casefold()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _str_list(raw: Any, key: str, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise AllowlistError(f"{where}: {key} must be a non-empty list of strings")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AllowlistError(f"{where}: {key} contains a non-string or empty value")
        out.append(item)
    if len(set(out)) != len(out):
        raise AllowlistError(f"{where}: {key} contains duplicates")
    return tuple(out)


def _ident(raw: Any, key: str, where: str) -> str:
    if not isinstance(raw, str) or not _ID_RE.match(raw):
        raise AllowlistError(f"{where}: {key} must be an alphanumeric GTFS id")
    return raw


def _number(raw: Any, key: str, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AllowlistError(f"{where}: {key} must be a number")
    value = float(raw)
    if not math.isfinite(value):
        raise AllowlistError(f"{where}: {key} must be finite")
    return value


def _parse_entry(raw: Any, index: int) -> AllowlistEntry:
    where = f"platform[{index}]"
    if not isinstance(raw, dict):
        raise AllowlistError(f"{where}: must be a table")
    keys = set(raw)
    if missing := _ENTRY_KEYS - keys:
        raise AllowlistError(f"{where}: missing keys {sorted(missing)}")
    if unknown := keys - _ENTRY_KEYS:
        raise AllowlistError(f"{where}: unknown keys {sorted(unknown)}")

    route_ids = _str_list(raw["route_ids"], "route_ids", where)
    for rid in route_ids:
        _ident(rid, "route_ids", where)
    lines = _str_list(raw["directions_line"], "directions_line", where)
    if any(normalize_line(v) != v for v in lines):
        raise AllowlistError(f"{where}: directions_line values must be pre-normalized")
    names = _str_list(raw["directions_names"], "directions_names", where)
    parent = _ident(raw["parent_station"], "parent_station", where)

    stop_name = raw["stop_name"]
    if not isinstance(stop_name, str) or not stop_name.strip():
        raise AllowlistError(f"{where}: stop_name must be a non-empty string")
    lat = _number(raw["stop_lat"], "stop_lat", where)
    lon = _number(raw["stop_lon"], "stop_lon", where)
    if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1] and _LON_RANGE[0] <= lon <= _LON_RANGE[1]):
        raise AllowlistError(f"{where}: coordinates ({lat}, {lon}) are outside NYC")
    max_d = _number(raw["max_coord_distance_m"], "max_coord_distance_m", where)
    if not 0 < max_d <= 500:
        raise AllowlistError(f"{where}: max_coord_distance_m must be in (0, 500]")

    plats = raw["platforms"]
    if not isinstance(plats, dict) or set(plats) != set(_DIRECTIONS):
        raise AllowlistError(f"{where}: platforms must have exactly keys N and S")
    platforms: dict[str, str] = {}
    for direction in _DIRECTIONS:
        pid = _ident(plats[direction], f"platforms.{direction}", where)
        if pid != parent + direction:
            raise AllowlistError(
                f"{where}: platform {direction}={pid!r} is not {parent + direction!r}"
            )
        platforms[direction] = pid

    return AllowlistEntry(
        route_ids=route_ids,
        directions_line=lines,
        parent_station=parent,
        stop_name=stop_name,
        stop_lat=lat,
        stop_lon=lon,
        platforms=platforms,
        directions_names=names,
        max_coord_distance_m=max_d,
    )


def parse_allowlist(text: str) -> tuple[AllowlistEntry, ...]:
    """Parse and validate allowlist TOML text."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AllowlistError(f"invalid TOML: {exc}") from exc
    if unknown := set(doc) - _TOP_KEYS:
        raise AllowlistError(f"unknown top-level keys {sorted(unknown)}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise AllowlistError(f"schema_version must be {SCHEMA_VERSION}")
    raw_entries = doc.get("platform")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise AllowlistError("at least one [[platform]] entry is required")

    entries = tuple(_parse_entry(raw, i) for i, raw in enumerate(raw_entries))
    parents = [e.parent_station for e in entries]
    if len(set(parents)) != len(parents):
        raise AllowlistError("duplicate parent_station entries")
    return entries


def load_allowlist(path: Path | None = None) -> tuple[AllowlistEntry, ...]:
    """Load the allowlist from ``path`` or the bundled package resource."""
    try:
        if path is None:
            text = (
                resources.files("commutecompass")
                .joinpath("data", ALLOWLIST_RESOURCE)
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AllowlistError(f"cannot read allowlist: {exc}") from exc
    return parse_allowlist(text)
