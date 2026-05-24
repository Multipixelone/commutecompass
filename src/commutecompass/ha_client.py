"""Home Assistant REST client — pulls tracker state and zones."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import httpx

from commutecompass.models import CurrentLocation, ZoneInfo
from commutecompass.timeutil import NYC_TZ, now_nyc

_logger = logging.getLogger(__name__)


def fetch_location(
    base_url: str,
    entity_id: str,
    token: str,
    *,
    timeout: float = 5.0,
    min_accuracy_m: Optional[float] = None,
) -> Optional[CurrentLocation]:
    """Fetch current location from a Home Assistant tracker entity.

    Works for `device_tracker.*` or `person.*` — both expose
    `attributes.latitude`/`longitude` and a state of the zone friendly_name.

    Returns None on any HTTP/parse failure, when the entity has no numeric
    coords, or when `gps_accuracy` is fuzzier than `min_accuracy_m` (when set).
    """
    if not (base_url and entity_id and token):
        return None

    url = f"{base_url.rstrip('/')}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning("HA fetch failed for %s: %s", entity_id, exc)
        return None

    if response.status_code != 200:
        _logger.warning(
            "HA fetch returned %d for %s: %s",
            response.status_code,
            entity_id,
            response.text[:200],
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        _logger.warning("HA fetch: bad JSON for %s: %s", entity_id, exc)
        return None

    if not isinstance(payload, dict):
        return None

    attrs = payload.get("attributes", {})
    if not isinstance(attrs, dict):
        return None

    lat = attrs.get("latitude")
    lon = attrs.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None

    accuracy_raw = attrs.get("gps_accuracy")
    accuracy_m: Optional[float] = (
        float(accuracy_raw) if isinstance(accuracy_raw, (int, float)) else None
    )
    if (
        min_accuracy_m is not None
        and accuracy_m is not None
        and accuracy_m > min_accuracy_m
    ):
        _logger.debug(
            "HA fetch: reject %s — accuracy %.0fm > %.0fm threshold",
            entity_id,
            accuracy_m,
            min_accuracy_m,
        )
        return None

    state = payload.get("state")
    zone = state if isinstance(state, str) and state else None

    captured_at = _parse_last_updated(payload.get("last_updated"))

    return CurrentLocation(
        lat=float(lat),
        lon=float(lon),
        zone=zone,
        captured_at=captured_at,
        source="home_assistant",
        accuracy_m=accuracy_m,
    )


def fetch_zones(
    base_url: str,
    token: str,
    *,
    timeout: float = 5.0,
) -> dict[str, ZoneInfo]:
    """Fetch HA zone entities, keyed by lower-cased friendly_name.

    Returns an empty dict on any HTTP/parse failure. Zones with non-numeric
    latitude/longitude are skipped.
    """
    if not (base_url and token):
        return {}

    url = f"{base_url.rstrip('/')}/api/states"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning("HA fetch_zones failed: %s", exc)
        return {}

    if response.status_code != 200:
        _logger.warning(
            "HA fetch_zones returned %d: %s",
            response.status_code,
            response.text[:200],
        )
        return {}

    try:
        payload = response.json()
    except ValueError as exc:
        _logger.warning("HA fetch_zones: bad JSON: %s", exc)
        return {}

    if not isinstance(payload, list):
        return {}

    zones: dict[str, ZoneInfo] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        entity_id = entry.get("entity_id", "")
        if not isinstance(entity_id, str) or not entity_id.startswith("zone."):
            continue
        attrs = entry.get("attributes", {})
        if not isinstance(attrs, dict):
            continue
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        radius = attrs.get("radius", 0.0)
        radius_m = float(radius) if isinstance(radius, (int, float)) else 0.0
        friendly = attrs.get("friendly_name") or entity_id.removeprefix("zone.")
        if not isinstance(friendly, str) or not friendly:
            continue
        zones[friendly.lower()] = ZoneInfo(
            name=friendly,
            lat=float(lat),
            lon=float(lon),
            radius_m=radius_m,
            entity_id=entity_id,
        )

    return zones


def _parse_last_updated(raw: object) -> datetime:
    """Parse HA's ISO-8601 last_updated; fall back to now_nyc() on failure."""
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return now_nyc()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NYC_TZ)
        return dt.astimezone(NYC_TZ)
    return now_nyc()
