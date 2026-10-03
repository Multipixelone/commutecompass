"""Real-time departure buffer from MTA GTFS-RT trip-update feeds.

Planning fails open (zero padding on unknown/error); alarm adjustment eligibility
fails closed. A measured delay requires independently validated GTFS boarding
identity AND a departure's explicit delay/time agreeing with the planned schedule.
Directions alone does not supply that identity. Nearest absolute departures,
fuzzy station names and headsigns must never establish a measured delay.
Bundled stop names can support informational departures, not alarm adjustment.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Callable, Literal, NamedTuple, Optional, TypedDict

import httpx
from rapidfuzz import fuzz, process

from commutecompass.config import RealtimeConfig
from commutecompass.gtfs_rt import fetch_feed_message
from commutecompass.models import Route, TransitLeg
from commutecompass.retry import retry
from commutecompass.timeutil import NYC_TZ

logger = logging.getLogger(__name__)

__all__ = ["RealtimeDelay", "realtime_delay"]


class RealtimeDelay(NamedTuple):
    minutes: int
    reason: Optional[str]  # e.g. "Q running ~6 min late", or None when on time
    status: Literal[
        "observed", "unavailable", "unmatched", "not_applicable", "cancelled", "skipped"
    ] = "not_applicable"
    detail: Optional[str] = None
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
    direction_id: Optional[int]
    stop_sequence: Optional[int]
    departure: Optional[datetime]
    arrival: Optional[datetime]
    delay: Optional[int]  # None is not the same as an explicit zero
    trip_relationship: int
    stop_relationship: int
    header_timestamp: Optional[int]
    update_timestamp: Optional[int]
    fresh: bool
    arrival_delay: Optional[int] = None
    trip_delay: Optional[int] = None


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
    fetcher: Optional[Fetcher] = None,
    clock: Optional[Clock] = None,
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
    except Exception as exc:  # fail-open: never let real-time break a plan
        logger.debug("realtime delay failed: %s", exc)
        return RealtimeDelay(0, None, "unavailable", "feed fetch or parse failed", feed_failed=True)


def _compute(
    route: Route, config: RealtimeConfig, fetcher: Optional[Fetcher], clock: Clock
) -> RealtimeDelay:
    transit_legs = [leg for leg in route.legs if leg.mode == "TRANSIT"]
    if not transit_legs:
        return _CLEAR

    # Only the first boarding leg is actionable by leaving earlier; downstream
    # delays are unavoidable and belong to the (deferred) reschedule case.
    leg = transit_legs[0]
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
            if _same_boarding(pred, leg):
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
    if pred.departure is None or pred.delay is None:
        return RealtimeDelay(0, None, "unmatched", "departure time and explicit delay required", **common)
    # Explicit delay provides a scheduled timestamp, not a nearest-event guess.
    # Delay is elapsed seconds, not wall-clock arithmetic across DST changes.
    if (pred.departure.astimezone(UTC) - timedelta(seconds=pred.delay)
            != leg.depart_at.astimezone(UTC)):
        return RealtimeDelay(0, None, "unmatched", "scheduled boarding timestamp differs", **common)
    delay_minutes = int(round(max(0, pred.delay) / 60.0))
    if delay_minutes == 0 or delay_minutes < config.min_delay_minutes:
        return RealtimeDelay(
            0, None, "observed", f"measured departure delay {pred.delay} sec; no padding", **common
        )
    delay_minutes = min(delay_minutes, config.max_buffer_minutes)
    return RealtimeDelay(delay_minutes, f"{leg.line} running ~{delay_minutes} min late", "observed", **common)


def _same_boarding(pred: Prediction, leg: TransitLeg) -> bool:
    """Require independently validated identity; no name/headsign inference."""
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
    urls: list[str], system: str, *, clock: Optional[Clock] = None,
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
            except Exception as exc:
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
            except Exception as exc:
                logger.debug("realtime feed parse failed (%s): %s", url, exc)
                result.failures += 1
    return result


def _fresh(header: Optional[int], update: Optional[int], now: datetime) -> bool:
    if header is None:
        return False
    for timestamp in (header, update):
        if timestamp is None:
            continue
        age = now.timestamp() - timestamp
        if age > _MAX_OBSERVATION_AGE_SECONDS or age < -_MAX_FUTURE_SKEW_SECONDS:
            return False
    return True


def _accumulate(feed: Any, preds: Predictions, system: str, *, now: Optional[datetime] = None) -> bool:
    observed_at = now or datetime.now(NYC_TZ)
    header_ts = feed.header.timestamp if feed.header.HasField("timestamp") else None
    # Differential feeds require state reconciliation, which we do not have.
    full = feed.header.incrementality == 0
    for entity in feed.entity:
        if entity.is_deleted or not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        update_ts = tu.timestamp if tu.HasField("timestamp") else None
        if tu.trip.schedule_relationship == 3 and not tu.stop_time_update:
            # Cancellation can be trip-level with no stop updates. Retain it;
            # only exact independently supplied trip/service identity can use it.
            preds.setdefault("", []).append(Prediction(
                "", tu.trip.route_id, tu.trip.trip_id, tu.trip.start_date, tu.trip.start_time,
                tu.trip.direction_id if tu.trip.HasField("direction_id") else None,
                None, None, None, None, 3, 0, header_ts, update_ts,
                full and _fresh(header_ts, update_ts, observed_at),
                trip_delay=tu.delay if tu.HasField("delay") else None,
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
                trip_delay=tu.delay if tu.HasField("delay") else None,
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
