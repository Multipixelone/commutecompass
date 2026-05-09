"""Home Assistant REST client — pulls device_tracker state."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import httpx

from commutecompass.models import CurrentLocation
from commutecompass.timeutil import NYC_TZ, now_nyc

_logger = logging.getLogger(__name__)


def fetch_location(
    base_url: str,
    entity_id: str,
    token: str,
    *,
    timeout: float = 5.0,
) -> Optional[CurrentLocation]:
    """Fetch current location from a Home Assistant device_tracker entity.

    Returns None on any HTTP/parse failure or when the entity has no
    numeric latitude/longitude attributes.
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

    state = payload.get("state")
    zone = state if isinstance(state, str) and state else None

    captured_at = _parse_last_updated(payload.get("last_updated"))

    return CurrentLocation(
        lat=float(lat),
        lon=float(lon),
        zone=zone,
        captured_at=captured_at,
        source="home_assistant",
    )


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
