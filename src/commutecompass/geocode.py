"""Google Geocoding API wrapper."""

from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel

from commutecompass.retry import retry


class GeocodeResult(BaseModel):
    """Result from the geocoder."""

    formatted_address: str
    lat: float
    lon: float
    place_id: Optional[str] = None


# NYC metro bounding box for region biasing
_NYC_BOUNDS = "40.5,-74.3|41.0,-73.7"


def _do_geocode_request(address: str, api_key: str) -> httpx.Response:
    """One HTTP attempt — raises for status so the retry helper can see 5xx/429."""
    params = {
        "address": address,
        "key": api_key,
        "region": "us",
        "bounds": _NYC_BOUNDS,
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params,
        )
        response.raise_for_status()
    return response


def geocode(address: str, api_key: str) -> Optional[GeocodeResult]:
    """Geocode an address using Google Geocoding API.

    Returns None on ZERO_RESULTS. Raises RuntimeError on transport errors
    (connection failure, timeout, or non-OK HTTP response) after retries
    have been exhausted.
    """
    try:
        response = retry(
            lambda: _do_geocode_request(address, api_key),
            label=f"geocode({address!r})",
        )
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Geocoding request timed out for address '{address}'") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Geocoding request failed with HTTP {e.response.status_code} for address '{address}'"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Geocoding request failed for address '{address}': {e}") from e

    data = response.json()
    status = data.get("status", "")

    if status == "ZERO_RESULTS":
        return None

    if status != "OK":
        # Expose non-OK, non-ZERO_RESULTS statuses as runtime errors
        raise RuntimeError(f"Google Geocoding API returned status '{status}' for address '{address}'")

    results = data.get("results", [])
    if not results:
        return None

    top = results[0]
    location = top.get("geometry", {}).get("location", {})
    return GeocodeResult(
        formatted_address=top.get("formatted_address", ""),
        lat=location.get("lat", 0.0),
        lon=location.get("lng", 0.0),
        place_id=top.get("place_id"),
    )