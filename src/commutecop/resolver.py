"""Location resolution pipeline."""

from __future__ import annotations

from typing import Callable, Optional

from commutecop.models import GeocodeResult, ResolvedLocation
from commutecop.venues import VenueRegistry


def resolve(
    raw: Optional[str],
    *,
    venues: VenueRegistry,
    store: "Store",  # type: ignore[name-defined]
    geocoder: Callable[[str], Optional[GeocodeResult]],
    llm: "OpencodeGoClient",  # type: ignore[name-defined]
) -> Optional[ResolvedLocation]:
    """Resolve a raw location string through the resolution pipeline.

    Pipeline:
    1. Empty check
    2. Cache hit
    3. Venue match
    4. Address geocode
    5. LLM resolution
    """
    raise NotImplementedError()