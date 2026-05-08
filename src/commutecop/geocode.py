"""Google Geocoding API wrapper."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class GeocodeResult(BaseModel):
    """Result from the geocoder."""
    formatted_address: str
    lat: float
    lon: float
    place_id: Optional[str] = None


def geocode(address: str, api_key: str) -> Optional[GeocodeResult]:
    """Geocode an address using Google Geocoding API.

    Returns None on ZERO_RESULTS. Raises on transport errors.
    """
    raise NotImplementedError()