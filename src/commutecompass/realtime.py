"""Real-time departure buffer from MTA GTFS-RT trip-update feeds.

Planning fails open (zero padding on unknown/error); alarm adjustment eligibility
fails closed. A measured delay requires independently validated GTFS boarding
identity AND a departure's explicit delay/time agreeing with the planned schedule.
For NYCT feeds (no delay/stop_sequence), the delay is derived only for the exact
bridge-validated, assigned trip (see the NYCT adapter section).
Directions alone does not supply that identity. Nearest absolute departures,
fuzzy station names and headsigns must never establish a measured delay.
Bundled stop names can support informational departures, not alarm adjustment.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal, NamedTuple, TypedDict

import httpx
from rapidfuzz import fuzz, process

from commutecompass.config import RealtimeConfig
from commutecompass.gtfs_rt import fetch_feed_message
from commutecompass.models import Route, TransitLeg
from commutecompass.retry import retry
from commutecompass.timeutil import NYC_TZ

try:  # protobuf ships with gtfs-realtime-bindings; this API exists since 4.21.
    from google.protobuf.unknown_fields import (  # type: ignore[import-untyped]
        UnknownFieldSet as _UnknownFieldSet,
    )
except ImportError:  # pragma: no cover - fail closed: extension unreadable
    _UnknownFieldSet = None

logger = logging.getLogger(__name__)

__all__ = ["RealtimeDelay", "realtime_delay"]


class RealtimeDelay(NamedTuple):
    minutes: int
    reason: str | None  # e.g. "Q running ~6 min late", or None when on time
    status: Literal[
        "observed", "unavailable", "unmatched", "not_applicable", "cancelled", "skipped"
    ] = "not_applicable"
    detail: str | None = None
    departures: tuple[datetime, ...] = ()
    feed_failed: bool = False


_CLEAR = RealtimeDelay(0, None)


class _ObservationExtras(TypedDict):
    departures: tuple[datetime, ...]
    feed_failed: bool


# Map a route leg's ``system`` to its bundled stop table.
_STOPS_RESOURCE = {
    "MTA Subway": "stops_subway.csv",
    "LIRR": "stops_lirr.csv",
    "MTA Bus": "stops_bus.csv",
}


@dataclass(frozen=True)
class Prediction:
    stop_id: str
    route_id: str
    trip_id: str
    start_date: str
    start_time: str
    direction_id: int | None
    stop_sequence: int | None
    departure: datetime | None
    arrival: datetime | None
    delay: int | None  # None is not the same as an explicit zero
    trip_relationship: int
    stop_relationship: int
    header_timestamp: int | None
    update_timestamp: int | None
    fresh: bool
    arrival_delay: int | None = None
    trip_delay: int | None = None
    # Decoded NYCT TripDescriptor extension; None when the trip carries none.
    nyct: NyctTrip | None = None


@dataclass(frozen=True)
class NyctTrip:
    """``NyctTripDescriptor`` (TripDescriptor extension 1001), decoded by hand.

    ``nyct-subway.proto`` is not among our dependencies, so the extension
    arrives as an unknown field and is decoded from its wire bytes.
    ``is_assigned`` uses the proto2 default (False) when absent.
    ``direction`` is the raw enum (1 NORTH, 2 EAST, 3 SOUTH, 4 WEST) or None.
    ``malformed`` marks undecodable bytes; such trips never match.
    """

    is_assigned: bool = False
    direction: int | None = None
    malformed: bool = False


Predictions = dict[str, list[Prediction]]


@dataclass
class FetchResult:
    predictions: Predictions = field(default_factory=dict)
    failures: int = 0
    usable_feeds: int = 0


Fetcher = Callable[[list[str], str], FetchResult]
Clock = Callable[[], datetime]

# Producer observations must be recent relative to the actual observation
# clock, NEVER the requested/planned departure. Missing headers are retained
# for information but are ineligible; a missing TU timestamp uses the header.
_MAX_OBSERVATION_AGE_SECONDS = 300
_MAX_FUTURE_SKEW_SECONDS = 60

# Reuse a fetched feed set briefly so a multi-event run fetches once per system
# (mirrors poll._alerts_cache).  Only used on the default (non-injected) path.
_FEED_TTL_SECONDS = 60.0
_FETCH_BUDGET_SECONDS = 5.0
_REQUEST_BUDGET_SECONDS = 2.0
_feed_cache: dict[tuple[str, tuple[str, ...]], tuple[float, FetchResult]] = {}

# Lazily-loaded, memoized stop tables: system → (names, normalized_name → ids).
_stops_cache: dict[str, tuple[list[str], dict[str, list[str]]]] = {}


def realtime_delay(
    route: Route,
    at_time: datetime,
    config: RealtimeConfig,
    *,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
) -> RealtimeDelay:
    """Return extra buffer minutes (and a reason) for the boarding line's delay.

    ``minutes``/``reason`` remain the planner API; ``status`` distinguishes a
    measured observation from unavailable, unmatched or inapplicable service.
    ``at_time`` is a planning reference, not an observation clock. ``clock``
    controls freshness independently. Injected fetchers return ``FetchResult``.
    """
    if not config.enabled:
        return _CLEAR
    try:
        return _compute(route, config, fetcher, clock or (lambda: datetime.now(NYC_TZ)))
    except Exception as exc:  # noqa: BLE001 - fail-open: never let real-time break a plan
        logger.debug("realtime delay failed: %s", exc)
        return RealtimeDelay(0, None, "unavailable", "feed fetch or parse failed", feed_failed=True)


def _compute(
    route: Route, config: RealtimeConfig, fetcher: Fetcher | None, clock: Clock
) -> RealtimeDelay:
    transit_legs = [leg for leg in route.legs if leg.mode == "TRANSIT"]
    if not transit_legs:
        return _CLEAR

    # Only the first boarding leg is actionable by leaving earlier; downstream
    # delays are unavoidable and belong to the (deferred) reschedule case.
    leg = transit_legs[0]
    # Cached/estimated routes never carry trusted identity (planner strips it);
    # refuse them here too rather than relying on a stale start_date.
    if route.from_cache or route.approximate:
        return RealtimeDelay(0, None, "unmatched", "cached or estimated route has no boarding identity")
    if not leg.scheduled_departure_valid:
        return RealtimeDelay(0, None, "unmatched", "boarding schedule is untrusted")
    system = leg.system
    if system not in _STOPS_RESOURCE or not leg.departure_stop or not leg.line:
        return _CLEAR

    candidate_ids = (
        [leg.gtfs_boarding_stop_id] if leg.gtfs_boarding_stop_id else
        _match_stop_ids(system, leg.departure_stop, config.fuzzy_threshold)
    )
    if not candidate_ids:
        return RealtimeDelay(0, None, "unmatched", "station identity is ambiguous or unknown")

    urls = _feed_urls(system, config)
    if not urls:
        return RealtimeDelay(0, None, "not_applicable", "no configured feed coverage")

    fetch: Fetcher = fetcher or (lambda urls, system: _fetch_predictions(urls, system, clock=clock))
    if fetcher is None:
        result = _cached_fetch(urls, system, fetch)
    else:
        result = fetch(urls, system)

    if not result.usable_feeds:
        return RealtimeDelay(0, None, "unavailable", "no fresh full feed available",
                             feed_failed=bool(result.failures))
    now = clock()
    candidates: list[Prediction] = []
    departures: list[datetime] = []
    stale = False
    fresh = False
    for sid in [*candidate_ids, ""]:
        for pred in result.predictions.get(sid, []):
            if leg.gtfs_route_id:
                route_matches = pred.route_id == leg.gtfs_route_id
            else:
                route_matches = _route_matches(system, pred.route_id, leg.line)
            if not route_matches:
                continue
            if not pred.fresh or not _fresh(pred.header_timestamp, pred.update_timestamp, now):
                stale = True
                continue
            fresh = True
            if pred.trip_relationship == 0 and pred.stop_relationship == 0 and pred.departure:
                departures.append(pred.departure)
            # Exact trip instance + directional platform/sequence context is
            # required. Names, nearest departures and headsigns prove nothing.
            # NYCT identity checks are authoritative even when the producer
            # also supplies standard fields. Never let the generic matcher
            # override a shape/platform/extension direction veto.
            matches = (
                _nyct_same_boarding(pred, leg)
                if _is_nyct_prediction(pred, system) else _same_boarding(pred, leg)
            )
            if matches:
                candidates.append(pred)

    # ZoneInfo equality/hash/order ignore fold when tzinfo is shared.
    # Deduplicate and order by instant, retaining NYC-local display values.
    departures_by_instant = {
        departure.astimezone(UTC): departure.astimezone(NYC_TZ) for departure in departures
    }
    common: _ObservationExtras = {
        "departures": tuple(value for _, value in sorted(departures_by_instant.items())),
        "feed_failed": bool(result.failures)
    }
    if stale and not fresh:
        return RealtimeDelay(0, None, "unavailable", "producer observation is stale or untrusted", **common)
    if len(candidates) != 1:
        return RealtimeDelay(0, None, "unmatched", "no unambiguous scheduled boarding identity", **common)
    pred = candidates[0]
    if pred.trip_relationship == 3:  # CANCELED
        return RealtimeDelay(0, None, "cancelled", "matched trip is cancelled", **common)
    if pred.trip_relationship != 0 or pred.stop_relationship != 0:
        if pred.trip_relationship == 0 and pred.stop_relationship == 1:  # SKIPPED
            return RealtimeDelay(0, None, "skipped", "matched boarding stop is skipped", **common)
        return RealtimeDelay(0, None, "unmatched", "non-scheduled or NO_DATA update", **common)
    # D4: an unassigned NYCT trip's prediction is just the schedule.
    if pred.nyct is not None and (pred.nyct.malformed or not pred.nyct.is_assigned):
        return RealtimeDelay(0, None, "unmatched", "NYCT trip is not assigned to a train", **common)
    # Generic feeds require an explicit delay. NYCT always requires assigned
    # departure evidence, cross-checking any explicit delay against the schedule.
    if not _is_nyct_prediction(pred, system):
        if pred.departure is None or pred.delay is None:
            return RealtimeDelay(0, None, "unmatched", "departure time and explicit delay required", **common)
        # Explicit delay provides a scheduled timestamp, not a nearest-event guess.
        # Delay is elapsed seconds, not wall-clock arithmetic across DST changes.
        if (pred.departure.astimezone(UTC) - timedelta(seconds=pred.delay)
                != leg.depart_at.astimezone(UTC)):
            return RealtimeDelay(0, None, "unmatched", "scheduled boarding timestamp differs", **common)
        delay_seconds = pred.delay
    else:
        derived = nyct_derived_delay(pred, leg)
        if derived is None:
            return RealtimeDelay(
                0, None, "unmatched", "NYCT boarding time unavailable or inconsistent", **common
            )
        delay_seconds = derived
    delay_minutes = round(max(0, delay_seconds) / 60.0)
    if delay_minutes == 0 or delay_minutes < config.min_delay_minutes:
        return RealtimeDelay(
            0, None, "observed", f"measured departure delay {delay_seconds} sec; no padding", **common
        )
    delay_minutes = min(delay_minutes, config.max_buffer_minutes)
    return RealtimeDelay(delay_minutes, f"{leg.line} running ~{delay_minutes} min late", "observed", **common)


def _has_bridge_provenance(leg: TransitLeg) -> bool:
    """True only for identity produced by the static GTFS bridge."""
    provenance = leg.gtfs_provenance
    return provenance is not None and provenance.source == "gtfs_bridge"


def _same_boarding(pred: Prediction, leg: TransitLeg) -> bool:
    """Require independently validated identity; no name/headsign inference."""
    if not _has_bridge_provenance(leg):
        return False
    if not (
        leg.gtfs_trip_id and leg.gtfs_route_id and leg.gtfs_start_date
        and leg.gtfs_boarding_stop_id and leg.gtfs_boarding_stop_sequence is not None
        and leg.gtfs_direction_id is not None
    ):
        return False
    if (
        pred.trip_id != leg.gtfs_trip_id or pred.route_id != leg.gtfs_route_id
        or pred.start_date != leg.gtfs_start_date or pred.direction_id != leg.gtfs_direction_id
        or (leg.gtfs_start_time is not None and pred.start_time != leg.gtfs_start_time)
    ):
        return False
    # Trip-level cancellation has no boarding event to compare.
    if pred.trip_relationship == 3 and not pred.stop_id:
        return True
    return (
        pred.stop_id == leg.gtfs_boarding_stop_id
        and pred.stop_sequence == leg.gtfs_boarding_stop_sequence
    )


# ── NYCT adapter (design §6) ──────────────────────────────────────────────────
#
# NYCT subway feeds omit stop_sequence and delay, and their direction_id is
# unreliable (the L feed sends 0 for southbound trips). For a bridge-validated
# leg only, direction comes from the trip_id shape and the platform suffix
# (cross-checked with extension 1001 when present), and delay is derived from
# the exact trip's departure time against the validated static schedule.

# ``087600_N..S36R`` / ``064200_GS.N01R``: origin time, route, dots, direction.
_NYCT_SHAPE = re.compile(r"^[0-9]{6}_[0-9A-Z]{1,2}\.{1,2}([NS])")
_NYCT_EXTENSION_DIRECTION = {1: "N", 3: "S"}  # EAST/WEST never match a platform
_NYCT_DIRECTION_ID = {"N": 0, "S": 1}  # static trips.txt convention


def nyct_trip_direction(trip_id: str) -> Optional[Literal["N", "S"]]:
    """Return ``"N"``/``"S"`` from a NYCT RT trip_id shape, or None if unparseable."""
    match = _NYCT_SHAPE.match(trip_id)
    if match is None:
        return None
    return "N" if match.group(1) == "N" else "S"


def _is_nyct_prediction(pred: Prediction, system: str) -> bool:
    return system == "MTA Subway" and (
        pred.nyct is not None or nyct_trip_direction(pred.trip_id) is not None
    )


def _nyct_direction_agrees(pred: Prediction, leg: TransitLeg) -> bool:
    """Shape, platform suffix, extension (if present) and leg must all agree.

    ``TripDescriptor.direction_id`` is deliberately never consulted.
    """
    shape = nyct_trip_direction(pred.trip_id)
    if shape is None or leg.gtfs_direction_id != _NYCT_DIRECTION_ID[shape]:
        return False
    if pred.stop_id and pred.stop_id[-1] != shape:
        return False
    if pred.nyct is not None:
        if pred.nyct.malformed:
            return False
        if pred.nyct.direction is not None and (
            _NYCT_EXTENSION_DIRECTION.get(pred.nyct.direction) != shape
        ):
            return False
    return True


def _nyct_same_boarding(pred: Prediction, leg: TransitLeg) -> bool:
    """Authoritative trip/date/route/platform match for all NYCT observations."""
    if leg.system != "MTA Subway" or not _has_bridge_provenance(leg):
        return False
    if not (
        leg.gtfs_trip_id and leg.gtfs_route_id and leg.gtfs_start_date
        and leg.gtfs_boarding_stop_id and leg.gtfs_boarding_stop_sequence is not None
        and leg.gtfs_direction_id is not None
    ):
        return False
    if (
        pred.trip_id != leg.gtfs_trip_id or pred.route_id != leg.gtfs_route_id
        or pred.start_date != leg.gtfs_start_date
        or (leg.gtfs_start_time is not None and pred.start_time != leg.gtfs_start_time)
    ):
        return False
    if not _nyct_direction_agrees(pred, leg):
        return False
    # Trip-level cancellation has no boarding event to compare.
    if pred.trip_relationship == 3 and not pred.stop_id:
        return True
    if pred.stop_sequence is not None and pred.stop_sequence != leg.gtfs_boarding_stop_sequence:
        return False
    return pred.stop_id == leg.gtfs_boarding_stop_id


def nyct_derived_delay(pred: Prediction, leg: TransitLeg) -> Optional[int]:
    """Boarding delay in UTC seconds for a matched, assigned NYCT trip, else None.

    The leg's ``depart_at`` is the bridge-validated static departure. Arrival-only
    predictions are ineligible: no static arrival/departure equality is persisted.
    None for an unassigned or extension-less trip (D4), no predicted departure,
    or a producer delay that contradicts the derived value.
    """
    if not _nyct_same_boarding(pred, leg):
        return None
    if pred.nyct is None or pred.nyct.malformed or not pred.nyct.is_assigned:
        return None
    observed = pred.departure
    if observed is None:
        return None
    delay = int(round(observed.timestamp() - leg.depart_at.timestamp()))
    if pred.delay is not None and pred.delay != delay:
        return None
    return delay


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _decode_nyct_descriptor(data: bytes, into: dict[str, Any]) -> None:
    """Merge NyctTripDescriptor wire bytes into ``into`` (last scalar wins)."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
            if number == 2:
                into["is_assigned"] = bool(value)
            elif number == 3:
                into["direction"] = value
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if pos > len(data):
            raise ValueError("truncated field")


def _nyct_trip(trip: Any) -> Optional[NyctTrip]:
    """Decode extension 1001 from a TripDescriptor's unknown fields, if present."""
    if _UnknownFieldSet is None:  # pragma: no cover - protobuf < 4.21
        return NyctTrip(malformed=True)
    fields: dict[str, Any] = {}
    present = False
    try:
        for unknown in _UnknownFieldSet(trip):
            if unknown.field_number != 1001:
                continue
            present = True
            if unknown.wire_type != 2:
                raise ValueError("extension 1001 is not length-delimited")
            _decode_nyct_descriptor(bytes(unknown.data), fields)
    except ValueError as exc:
        logger.debug("malformed NYCT trip descriptor: %s", exc)
        return NyctTrip(malformed=True)
    return NyctTrip(**fields) if present else None


# ── stop-name → stop_id matching ──────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace for fuzzy comparison."""
    out: list[str] = []
    prev_space = False
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def _load_stops(system: str) -> tuple[list[str], dict[str, list[str]]]:
    cached = _stops_cache.get(system)
    if cached is not None:
        return cached

    resource = _STOPS_RESOURCE[system]
    text = resources.files("commutecompass").joinpath("data", resource).read_text(encoding="utf-8")
    name_to_ids: dict[str, list[str]] = {}
    for row in csv.DictReader(text.splitlines()):
        stop_id = (row.get("stop_id") or "").strip()
        name = _normalize(row.get("stop_name") or "")
        if not stop_id or not name:
            continue
        name_to_ids.setdefault(name, []).append(stop_id)

    result = (list(name_to_ids.keys()), name_to_ids)
    _stops_cache[system] = result
    return result


def _match_stop_ids(system: str, stop_name: str, threshold: int) -> list[str]:
    names, name_to_ids = _load_stops(system)
    if not names:
        return []
    query = _normalize(stop_name)
    if not query:
        return []
    match = process.extractOne(query, names, scorer=fuzz.WRatio, score_cutoff=threshold)
    if match is None:
        return []
    matched_name: str = match[0]
    ids = name_to_ids.get(matched_name, [])
    # The bundled tables lack coordinates: same-name distinct stations cannot
    # be geographically discriminated here. Do not pool them.
    return _expand_ids(system, ids) if len(ids) == 1 else []


def _expand_ids(system: str, base_ids: list[str]) -> list[str]:
    """Reconstruct the feed's stop ids from the bundled (parent) ids.

    Subway feeds key stop-time updates on directional platform ids (``R20N`` /
    ``R20S``) whose parent station is ``R20``; we keep the parent in the CSV and
    expand here.  LIRR/bus ids are already flat.
    """
    if system != "MTA Subway":
        return list(base_ids)
    expanded: list[str] = []
    for sid in base_ids:
        expanded.extend((sid, f"{sid}N", f"{sid}S"))
    return expanded


# ── route_id matching (per-system conventions) ───────────────────────────────


def _route_matches(system: str, feed_route_id: str, leg_line: str) -> bool:
    if system == "LIRR":
        # No validated Directions branch → GTFS route mapping is available.
        return False
    line = leg_line.upper().strip().replace(" ", "")
    rid = feed_route_id.upper().strip()
    if system == "MTA Bus":
        # OneBusAway route ids look like "MTA NYCT_M15" / "MTABC_Q44".
        if "_" in rid:
            rid = rid.split("_", 1)[1]
        return rid.replace(" ", "") == line
    # Subway: route_id is the line designator; tolerate express suffix ("6X").
    return rid == line or rid.rstrip("X") == line


# ── feed fetching ─────────────────────────────────────────────────────────────


def _feed_urls(system: str, config: RealtimeConfig) -> list[str]:
    if system == "MTA Subway":
        return [u for u in config.subway_tripupdate_urls if u]
    if system == "LIRR":
        return [config.lirr_tripupdate_url] if config.lirr_tripupdate_url else []
    if system == "MTA Bus":
        return [config.bus_tripupdate_url] if config.bus_tripupdate_url else []
    return []


def _normalize_feed_stop(stop_id: str, system: str) -> str:
    if system == "MTA Bus":
        for prefix in ("MTA_", "MTABC_"):
            if stop_id.startswith(prefix):
                return stop_id[len(prefix) :]
    return stop_id


def _fetch_predictions(
    urls: list[str], system: str, *, clock: Clock | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FetchResult:
    """Fetch sequentially within a five-second admission budget per feed set.

    Each attempt divides min(2s, remaining budget) across HTTPX's four timeout
    phases. Retry once without backoff, checking the deadline again first.
    HTTPX timeouts are inactivity/phase limits, not a hard wall-clock deadline
    for streaming responses or parsing; no further I/O starts after expiry.
    """
    result = FetchResult()
    observation_clock = clock or (lambda: datetime.now(NYC_TZ))
    deadline = monotonic() + _FETCH_BUDGET_SECONDS
    with httpx.Client(timeout=_REQUEST_BUDGET_SECONDS / 4) as client:
        for index, url in enumerate(urls):
            if not url:
                continue
            if monotonic() >= deadline:
                result.failures += sum(bool(u) for u in urls[index:])
                logger.debug("realtime fetch budget exhausted (%s)", system)
                break

            def _do(u: str = url) -> Any:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("realtime fetch budget exhausted")
                client.timeout = httpx.Timeout(min(_REQUEST_BUDGET_SECONDS, remaining) / 4)
                return fetch_feed_message(u, system, client)

            try:
                feed = retry(_do, attempts=2, base_delay=0, max_delay=0,
                             label=f"realtime({system})")
            except Exception as exc:  # noqa: BLE001 - fail-open per feed
                logger.debug("realtime feed fetch failed (%s): %s", url, exc)
                result.failures += 1
                continue
            try:
                # Parsing is atomic per feed: a malformed later entity must not
                # leave earlier predictions eligible in a mixed feed result.
                predictions: Predictions = {}
                if _accumulate(feed, predictions, system, now=observation_clock()):
                    result.usable_feeds += 1
                for stop_id, updates in predictions.items():
                    result.predictions.setdefault(stop_id, []).extend(updates)
            except Exception as exc:  # noqa: BLE001 - fail-open per feed
                logger.debug("realtime feed parse failed (%s): %s", url, exc)
                result.failures += 1
    return result


def _fresh(header: int | None, update: int | None, now: datetime) -> bool:
    if header is None:
        return False
    for timestamp in (header, update):
        if timestamp is None:
            continue
        age = now.timestamp() - timestamp
        if age > _MAX_OBSERVATION_AGE_SECONDS or age < -_MAX_FUTURE_SKEW_SECONDS:
            return False
    return True


def _accumulate(feed: Any, preds: Predictions, system: str, *, now: datetime | None = None) -> bool:
    observed_at = now or datetime.now(NYC_TZ)
    header_ts = feed.header.timestamp if feed.header.HasField("timestamp") else None
    # Differential feeds require state reconciliation, which we do not have.
    full = feed.header.incrementality == 0
    for entity in feed.entity:
        if entity.is_deleted or not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        update_ts = tu.timestamp if tu.HasField("timestamp") else None
        nyct = _nyct_trip(tu.trip) if system == "MTA Subway" else None
        if tu.trip.schedule_relationship == 3 and not tu.stop_time_update:
            # Cancellation can be trip-level with no stop updates. Retain it;
            # only exact independently supplied trip/service identity can use it.
            preds.setdefault("", []).append(Prediction(
                "", tu.trip.route_id, tu.trip.trip_id, tu.trip.start_date, tu.trip.start_time,
                tu.trip.direction_id if tu.trip.HasField("direction_id") else None,
                None, None, None, None, 3, 0, header_ts, update_ts,
                full and _fresh(header_ts, update_ts, observed_at),
                trip_delay=tu.delay if tu.HasField("delay") else None, nyct=nyct,
            ))
        for stu in tu.stop_time_update:
            stop_id = stu.stop_id
            if not stop_id:
                continue
            departure = (datetime.fromtimestamp(stu.departure.time, tz=NYC_TZ)
                         if stu.HasField("departure") and stu.departure.HasField("time") else None)
            arrival = (datetime.fromtimestamp(stu.arrival.time, tz=NYC_TZ)
                       if stu.HasField("arrival") and stu.arrival.HasField("time") else None)
            delay = (stu.departure.delay if stu.HasField("departure")
                     and stu.departure.HasField("delay") else None)
            key = _normalize_feed_stop(stop_id, system)
            preds.setdefault(key, []).append(Prediction(
                key, tu.trip.route_id, tu.trip.trip_id, tu.trip.start_date, tu.trip.start_time,
                tu.trip.direction_id if tu.trip.HasField("direction_id") else None,
                stu.stop_sequence if stu.HasField("stop_sequence") else None,
                departure, arrival, delay, tu.trip.schedule_relationship, stu.schedule_relationship,
                header_ts, update_ts, full and _fresh(header_ts, update_ts, observed_at),
                arrival_delay=(stu.arrival.delay if stu.HasField("arrival")
                               and stu.arrival.HasField("delay") else None),
                trip_delay=tu.delay if tu.HasField("delay") else None, nyct=nyct,
            ))
    return full and _fresh(header_ts, None, observed_at)


def _cached_fetch(
    urls: list[str], system: str, fetcher: Fetcher, *, monotonic: Callable[[], float] = time.monotonic
) -> FetchResult:
    key = (system, tuple(urls))
    now = monotonic()
    hit = _feed_cache.get(key)
    if hit is not None and now - hit[0] < _FEED_TTL_SECONDS:
        return hit[1]
    preds = fetcher(urls, system)
    _feed_cache[key] = (monotonic(), preds)
    return preds
