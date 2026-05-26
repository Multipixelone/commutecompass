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


def call_service(
    base_url: str,
    token: str,
    domain: str,
    service: str,
    data: dict[str, object] | None = None,
    *,
    timeout: float = 5.0,
) -> bool:
    """POST to /api/services/{domain}/{service}.

    Used to trigger HA-side automations, scripts, or notify services (e.g.
    ``notify.mobile_app_iphone``, ``script.commute_alarm``).  Returns True on
    HTTP 2xx; False on any failure (logged at WARNING).  Never raises.
    """
    if not (base_url and domain and service and token):
        return False

    url = f"{base_url.rstrip('/')}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = dict(data) if data else {}

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning("HA call_service %s.%s failed: %s", domain, service, exc)
        return False

    if not (200 <= response.status_code < 300):
        _logger.warning(
            "HA call_service %s.%s returned %d: %s",
            domain,
            service,
            response.status_code,
            response.text[:200],
        )
        return False

    return True


def push_tomorrow_alarm(
    base_url: str,
    token: str,
    service: str,
    alarm_at: datetime,
    *,
    extra_data: Optional[dict[str, object]] = None,
    timeout: float = 5.0,
) -> bool:
    """POST tomorrow's alarm time to an HA script as ``domain.service``.

    Body: ``{"datetime": "<iso-8601 NYC-local>", **extra_data}``. The HA
    script is expected to copy that value into an ``input_datetime`` helper
    that an iOS Shortcut polls each evening. Returns the underlying
    ``call_service`` result (False on any failure; never raises).

    ``service`` must be of the form ``"domain.service"`` (e.g.
    ``script.commute_set_tomorrow_alarm``). Returns False for malformed
    service names.

    Field name: the payload key is ``alarm_at`` rather than ``datetime``
    because ``datetime`` is a reserved Jinja namespace in Home Assistant
    (it resolves to the Python module). The matching HA script must declare
    a field called ``alarm_at`` and reference it as ``{{ alarm_at }}``.
    """
    if not (base_url and token and service):
        return False
    domain, sep, name = service.partition(".")
    if not sep or not domain or not name:
        _logger.warning(
            "push_tomorrow_alarm: service %r is not 'domain.service' — skipping",
            service,
        )
        return False

    payload: dict[str, object] = {"alarm_at": alarm_at.isoformat()}
    if extra_data:
        payload.update(extra_data)
    return call_service(
        base_url, token, domain, name, data=payload, timeout=timeout
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
