"""MTA GTFS-RT alert fetcher and matcher."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

import httpx
from google.transit.gtfs_realtime_pb2 import (  # type: ignore[import-untyped]
    FeedMessage,
    Alert as GtfsAlert,
)

from commutecompass.models import Alert, Route, TransitLeg
from commutecompass.timeutil import NYC_TZ

if TYPE_CHECKING:
    from commutecompass.llm import OpencodeGoClient

logger = logging.getLogger(__name__)

# Canonical MTA GTFS-RT alert feed URLs (verify against https://api.mta.info)
MTA_SUBWAY_ALERTS_URL = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"
)
MTA_LIRR_ALERTS_URL = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Flirr-alerts"
)
MTA_BUS_ALERTS_URL = "https://gtfsrt.prod.obanyc.com/alerts"

# Number of seconds to add to current time for "no expiry" sentinel
_FAR_FUTURE_SECONDS = 2**30


def fetch_alerts(
    subway_url: str,
    lirr_url: str,
    bus_url: str,
    client: Optional[httpx.Client] = None,
) -> list[Alert]:
    """Fetch and parse MTA GTFS-RT alert feeds from all sources.

    Args:
        subway_url: GTFS-RT protobuf URL for subway alerts.
        lirr_url: GTFS-RT protobuf URL for LIRR alerts.
        bus_url: GTFS-RT protobuf URL for bus alerts.
        client: Optional httpx.Client for test injection.

    Returns:
        List of parsed Alert models from all feeds.
    """
    feed_urls = [
        (subway_url or MTA_SUBWAY_ALERTS_URL, "MTA Subway"),
        (lirr_url or MTA_LIRR_ALERTS_URL, "LIRR"),
        (bus_url or MTA_BUS_ALERTS_URL, "MTA Bus"),
    ]

    alerts: list[Alert] = []
    close_client = False

    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        for url, system in feed_urls:
            try:
                alerts.extend(_fetch_feed(url, system, client))
            except Exception as exc:
                logger.warning("Failed to fetch %s alerts from %s: %s", system, url, exc)

    finally:
        if close_client:
            client.close()

    return alerts


def _fetch_feed(url: str, system: str, client: httpx.Client) -> list[Alert]:
    """Fetch and parse a single GTFS-RT feed."""
    response = client.get(url)
    response.raise_for_status()

    # Diagnostic: detect non-protobuf payloads before attempting parse
    content = response.content
    if not content:
        raise ValueError(
            f"Empty response body from {system} feed ({url}): "
            f"status={response.status_code}, content_type={response.headers.get('content-type', 'unknown')}"
        )

    preview = content[:200]
    if content.startswith(b"<?xml") or content.startswith(b"<Error") or content.startswith(b"<error"):
        raise ValueError(
            f"MTA feed {system} ({url}) returned XML/HTML instead of protobuf: "
            f"status={response.status_code}, content_type={response.headers.get('content-type', 'unknown')}, "
            f"preview={preview[:80]!r}"
        )
    if content.startswith((b"{", b"[")):
        raise ValueError(
            f"MTA feed {system} ({url}) returned JSON instead of protobuf: "
            f"status={response.status_code}, content_type={response.headers.get('content-type', 'unknown')}, "
            f"preview={preview[:80]!r}"
        )

    feed = FeedMessage.FromString(content)

    alerts: list[Alert] = []
    for entity in feed.entity:
        if entity.HasField("alert"):
            parsed = _parse_alert(entity.alert, system)
            if parsed:
                alerts.append(parsed)

    return alerts


def _parse_alert(gtfs_alert: GtfsAlert, system: str) -> Optional[Alert]:
    """Map a GTFS-RT Alert proto into our Alert model."""
    if not gtfs_alert.informed_entity:
        return None

    # Collect affected route IDs from all informed entities
    affected_routes: set[str] = set()
    affected_systems: set[str] = {system}

    for entity in gtfs_alert.informed_entity:
        if entity.HasField("route_id") and entity.route_id:
            affected_routes.add(entity.route_id)

    # Parse active periods
    active_periods: list[tuple[datetime, datetime | None]] = []
    for period in gtfs_alert.active_period:
        start = _parse_timestamp(period.start)
        end = _parse_timestamp(period.end) if period.HasField("end") else None
        if start is not None:
            active_periods.append((start, end))

    if not active_periods:
        # No active period means currently active with no known end
        now = datetime.now(NYC_TZ)
        active_periods.append((now, None))

    # Derive severity from GTFS-RT severity_level field
    severity: Literal["INFO", "WARNING", "SEVERE"] = "INFO"
    if gtfs_alert.HasField("severity_level"):
        sev_val = gtfs_alert.severity_level
        # Enum values: UNKNOWN=0, INFO=1, WARNING=2, SEVERE=3
        if sev_val == 3:
            severity = "SEVERE"
        elif sev_val == 2:
            severity = "WARNING"
        elif sev_val == 1:
            severity = "INFO"

    # Extract header and description text (translated text proto)
    header = _extract_text(gtfs_alert.header_text) if gtfs_alert.HasField("header_text") else ""
    description = (
        _extract_text(gtfs_alert.description_text)
        if gtfs_alert.HasField("description_text")
        else ""
    )

    # Extract URL if present
    url: Optional[str] = None
    if gtfs_alert.HasField("url") and gtfs_alert.url.HasField("translation"):
        translations = gtfs_alert.url.translation
        if translations:
            url = translations[0].text if translations[0].text else None

    # Generate stable alert id from affected routes + start time of first period
    first_period = active_periods[0] if active_periods else (None, None)
    id_base = f"{system}:{','.join(sorted(affected_routes)) if affected_routes else 'unknown'}"
    if first_period[0]:
        id_base += f":{first_period[0].strftime('%Y%m%d%H%M')}"
    alert_id = id_base[:128]

    return Alert(
        id=alert_id,
        header=header or f"{system} alert",
        description=description,
        affected_routes=affected_routes,
        affected_systems=affected_systems,
        active_periods=active_periods,
        severity=severity,
        url=url,
    )


def _extract_text(text_proto: Any) -> str:
    """Extract string from a TranslatedString proto."""
    if not text_proto.translation:
        return ""
    for trans in text_proto.translation:
        if trans.text:
            return str(trans.text)
    return ""


def _parse_timestamp(seconds: int) -> Optional[datetime]:
    """Convert GTFS-RT Unix timestamp to aware NYC datetime."""
    if seconds == 0:
        return None
    try:
        dt = datetime.fromtimestamp(seconds, tz=NYC_TZ)
        return dt
    except (OSError, OverflowError):
        return None


def alerts_affecting_route(
    alerts: list[Alert],
    route: Route,
    at_time: datetime,
) -> list[Alert]:
    """Return alerts that affect the given route at the given time.

    An alert affects a route when ALL of the following hold:
    1. The route has at least one transit leg whose (system, line) overlaps
       with the alert's (affected_systems, affected_routes).
    2. The alert has at least one active period whose time window overlaps
       with the route's departure time ``at_time``.

    Args:
        alerts: List of parsed Alert models.
        route: The Route to check against.
        at_time: The departure time to check (used for active-period overlap).

    Returns:
        Filtered list of alerts that affect this route at this time.
    """
    if route.legs is None:
        return []

    affected: list[Alert] = []

    for alert in alerts:
        if _alert_affects_route(alert, route, at_time):
            affected.append(alert)

    return affected


_DISRUPTION_PATTERNS = (
    r"\bdelay(?:ed|s)?\b",
    r"\bservice\s+change\b",
    r"\bno\s+service\b",
    r"\bsuspend(?:ed|sion)?\b",
    r"\bpart(?:ial)?\s+suspend(?:ed|sion)?\b",
    r"\bslow(?:\s+zones?)?\b",
    r"\bplanned\s+work\b",
    r"\bre-rout(?:e|ed|ing)\b",
    r"\bskip(?:ping)?\s+stops?\b",
    r"\bexpress(?:\s+service)?\b",
    r"\blocal(?:\s+service)?\b",
    r"\bsignal\s+problem\b",
    r"\btrack\s+problem\b",
)

_NON_COMMUTE_PATTERNS = (
    r"\belevator\b",
    r"\bescalator\b",
    r"\baccessibil(?:ity|ities)\b",
    r"\bstation\s+agent\b",
    r"\bbooth\b",
    r"\bparking\b",
    r"\bticket\s+office\b",
)


def select_actionable_alerts(
    alerts: list[Alert],
    route: Route,
    at_time: datetime,
    *,
    llm: Optional["OpencodeGoClient"] = None,
) -> list[Alert]:
    """Return the subset of affecting alerts likely to impact this commute.

    This keeps route/time matching strict (via ``alerts_affecting_route``), then
    removes common non-commute alerts (e.g., elevator/escalator advisories), and
    optionally asks the LLM only for ambiguous cases.
    """
    affecting = alerts_affecting_route(alerts, route, at_time)
    selected: list[Alert] = []

    route_lines = {
        leg.line.lower().strip()
        for leg in route.legs
        if leg.mode == "TRANSIT" and leg.line
    }

    for alert in affecting:
        if _is_non_commute_alert(alert):
            logger.debug("Filtered non-commute alert %s", alert.id)
            continue

        decision = _heuristic_relevance_decision(alert, route_lines, route)
        if decision is True:
            selected.append(alert)
            continue
        if decision is False:
            continue

        if llm is not None:
            llm_decision = llm.classify_alert_relevance(alert, route, at_time=at_time)
            if llm_decision is True:
                selected.append(alert)
                continue
            if llm_decision is False:
                continue

        # Conservative fallback for ambiguous alerts when no LLM decision.
        if alert.severity in {"WARNING", "SEVERE"}:
            selected.append(alert)

    return selected


def _alert_text(alert: Alert) -> str:
    return f"{alert.header}\n{alert.description}".lower()


def _normalize_alert_text(text: str) -> str:
    """Strip common MTA alert noise to focus on substantive content."""
    text = re.sub(r"\s+", " ", text)
    return text


def _build_route_context(route: Route) -> tuple[set[str], set[str]]:
    """Build keyword sets from route transit legs to assess alert relevance.

    Returns:
        (stop_names, line_ids) extracted from the route's transit legs.
    """
    stop_names: set[str] = set()
    line_ids: set[str] = set()

    for leg in route.legs:
        if leg.mode != "TRANSIT":
            continue
        if leg.line:
            line_ids.add(leg.line.lower().strip())
        if leg.headsign:
            stop_names.add(leg.headsign.lower().strip())
        # Extract origin/destination stop names from summary (e.g. "C from A to B")
        if leg.summary:
            parts = leg.summary.split(" from ")
            if len(parts) >= 2:
                # left side is the line; right side is "A to B"
                right = parts[1]
                for stop in right.replace(" to ", " ").replace(" and ", " ").split():
                    stop = stop.strip(",. ")
                    if stop and stop not in ("to", "and"):
                        stop_names.add(stop.lower())

    return stop_names, line_ids


_LOCATION_SPECIFIC_PATTERNS = (
    # Alert header/description mentions specific stations or segments
    r"\bat\s+[\w\s' -]+station\b",
    r"\bbetween\s+[\w\s' -]+and\s+[\w\s' -]+\b",
    r"\bnear\s+[\w\s' -]+station\b",
    r"\bat\s+[\w\s' -]+\s+stop\b",
    r"\bfrom\s+[\w\s' -]+to\s+[\w\s' -]+\b",
)


def _is_location_specific_alert(
    alert: Alert,
    stop_names: set[str],
    line_ids: set[str],
) -> bool:
    """Return True if an alert's location references appear unrelated to route context.

    Checks whether an alert mentions specific stations or segments (via patterns like
    "at X station", "between X and Y") that have no overlap with the route's own
    stop names and headsigns.  This filters alerts for disruptions far from the
    rider's actual segment.
    """
    text = _alert_text(alert)

    # If no location-specific phrasing, leave it in
    if not _contains_any(text, _LOCATION_SPECIFIC_PATTERNS):
        return False

    # Check overlap with route context — split multi-word stop names into tokens,
    # but only count tokens >= 4 chars to avoid false positives from short
    # abbreviations like "st" that appear in many station names.
    stop_tokens = set()
    for stop in stop_names:
        for token in stop.split():
            if len(token) >= 4:
                stop_tokens.add(token)
    words_in_text = set(re.findall(r"\b[\w]+", text))

    overlap = stop_tokens & words_in_text
    if overlap:
        # Alert mentions at least one stop the route actually uses — keep it
        return False

    # No overlap: location-specific alert unrelated to this route
    return True


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pat, text) is not None for pat in patterns)


def _is_non_commute_alert(alert: Alert) -> bool:
    text = _alert_text(alert)
    if not _contains_any(text, _NON_COMMUTE_PATTERNS):
        return False
    # If it simultaneously contains disruption language, keep it.
    return not _contains_any(text, _DISRUPTION_PATTERNS)


def _heuristic_relevance_decision(
    alert: Alert,
    route_lines: set[str],
    route: Route,
) -> Optional[bool]:
    """Return True/False when heuristic is confident, else None.

    - True: explicit disruption wording that references route lines, or severe alert.
    - False: explicit non-commute advisory, or location-specific alert for unrelated stops.
    - None: uncertain/ambiguous.
    """
    text = _alert_text(alert)

    if _is_non_commute_alert(alert):
        return False

    # Build route context for location-specific filtering
    stop_names, line_ids = _build_route_context(route)

    # Filter far-away location-specific alerts
    if _is_location_specific_alert(alert, stop_names, line_ids):
        # Do NOT drop severe/system-wide alerts
        if alert.severity != "SEVERE" and alert.affected_routes != {"*"}:
            logger.debug("Filtered location-specific alert %s (no route stop overlap)", alert.id)
            return False

    severe = alert.severity == "SEVERE"
    has_disruption_words = _contains_any(text, _DISRUPTION_PATTERNS)
    route_mentioned = any(line in text for line in route_lines) if route_lines else False

    if severe and has_disruption_words:
        return True

    if has_disruption_words and route_mentioned:
        return True

    if has_disruption_words and alert.affected_routes:
        # Already route-matched upstream; disruption + explicit route IDs is enough.
        return True

    return None


def _alert_affects_route(alert: Alert, route: Route, at_time: datetime) -> bool:
    """Check if a single alert affects the route (system/line overlap + time overlap)."""
    # 1. System/line overlap check
    if not _systems_lines_overlap(alert, route):
        return False

    # 2. Time-window overlap check
    if not _time_overlaps(alert, route, at_time):
        return False

    return True


def _systems_lines_overlap(alert: Alert, route: Route) -> bool:
    """Check if any route leg's (system, line) intersects alert's affected routes/systems."""
    for leg in route.legs:
        if leg.mode != "TRANSIT":
            continue

        # Check system match
        if alert.affected_systems and leg.system:
            if leg.system in alert.affected_systems:
                # System matches — now check line/route overlap
                if _line_matches(alert, leg):
                    return True

        # Also check route-level: some alerts affect entire systems
        if alert.affected_systems and leg.system and "*" in alert.affected_routes:
            # Wildcard means the whole system is affected
            return True

    return False


def _line_matches(alert: Alert, leg: TransitLeg) -> bool:
    """Check if a transit leg's line/route matches alert's affected_routes."""
    if not alert.affected_routes:
        # No specific routes means whole system is affected
        return True

    if leg.line:
        # Direct line match
        if leg.line in alert.affected_routes:
            return True

    # Also check if any affected route is a substring of the line (route IDs
    # sometimes have prefixes like "ABC" for the C line)
    if leg.line:
        for affected in alert.affected_routes:
            if affected in leg.line:
                return True

    return False


def _time_overlaps(alert: Alert, route: Route, at_time: datetime) -> bool:
    """Check if any alert active period overlaps with the route's time window.

    For each transit leg in the route, we check if the alert's active period
    overlaps with the leg's time window (depart_at to arrive_at). If any leg
    overlaps, the alert is considered affecting the route.

    Args:
        alert: The Alert to check.
        route: The Route whose legs provide time windows.
        at_time: Reference time for checking alert active periods.

    Returns:
        True if any leg's time window overlaps any alert active period.
    """
    for leg in route.legs:
        if leg.mode != "TRANSIT":
            continue

        leg_start = _ensure_aware(leg.depart_at)
        leg_end = _ensure_aware(leg.arrive_at)

        for active_start, active_end in alert.active_periods:
            if _periods_overlap(leg_start, leg_end, active_start, active_end):
                return True

    return False


def _ensure_aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware in NYC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NYC_TZ)
    return dt.astimezone(NYC_TZ)


def _periods_overlap(
    start1: datetime,
    end1: datetime,
    start2: Optional[datetime],
    end2: Optional[datetime],
) -> bool:
    """Check if two time periods overlap (treating None as open-ended)."""
    # Handle open-ended periods
    # end1 None means open-ended (extends forever)
    # start2 None means starts in past (already active)
    # end2 None means no known end (still active)

    # Normalize: treat None start as -infinity, None end as +infinity
    s1, e1 = start1, end1 if end1 else datetime.max.replace(tzinfo=NYC_TZ)
    s2 = start2 if start2 else datetime.min.replace(tzinfo=NYC_TZ)
    e2 = end2 if end2 else datetime.max.replace(tzinfo=NYC_TZ)

    # Two periods overlap if one starts before the other ends
    return s1 < e2 and s2 < e1
