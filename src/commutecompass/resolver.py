"""Location resolution pipeline."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Callable, Optional

from commutecompass.geocode import GeocodeResult
from commutecompass.models import ResolvedLocation
from commutecompass.venues import VenueRegistry

if TYPE_CHECKING:
    from commutecompass.store import Store
    from commutecompass.llm import OpencodeGoClient

log = logging.getLogger(__name__)

# Street-type keywords for address heuristic
_STREET_WORDS = frozenset([
    "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road",
    "dr", "drive", "ln", "lane", "pl", "place", "way", "court", "ct",
    "circle", "cir", "park", "sq", "square", "broadway",
])


def looks_like_address(raw: str) -> bool:
    """Return True if `raw` looks like a street address.

    Heuristic: contains at least one digit AND at least one token that
    resembles a street type (e.g. "St", "Ave", "Broadway").
    """
    if not raw:
        return False
    has_digit = bool(re.search(r"\d", raw))
    tokens = re.findall(r"[A-Za-z]+", raw)
    has_street_word = any(t.lower() in _STREET_WORDS for t in tokens)
    return has_digit and has_street_word


_PLACEHOLDER_PATTERNS = (
    "location available once rsvp",
    "location revealed after rsvp",
    "rsvp to see location",
    "tba",
    "tbd",
    "tbd location",
)


def _is_placeholder(raw: str) -> bool:
    """Return True if `raw` is clearly a non-actionable placeholder string."""
    lowered = raw.lower()
    return any(pat in lowered for pat in _PLACEHOLDER_PATTERNS)


# ── Step 4b: placeholder check ────────────────────────────────────────────────


def resolve(
    raw: Optional[str],
    *,
    venues: VenueRegistry,
    store: "Store",
    geocoder: Callable[[str], Optional[GeocodeResult]],
    llm: "OpencodeGoClient",
) -> Optional[ResolvedLocation]:
    """Resolve a raw location string through the resolution pipeline.

    Pipeline (§6.10):
    1. Empty raw → None
    2. Cache hit (store.get_geocode) → return cached
    3. Venue registry match → cache + return
    4. looks_like_address → geocode → cache + return
    4b. Placeholder check → None (skip LLM/geocoder)
    5. LLM resolution → address: geocode + cache + return;
                        station: cache + return
    6. Unresolved → log + return None
    """
    # ── Step 1: empty check ────────────────────────────────────────────────────
    if not raw or not raw.strip():
        return None

    raw = raw.strip()

    # ── Step 2: cache hit ───────────────────────────────────────────────────────
    cached = store.get_geocode(raw)
    if cached is not None:
        log.debug("resolver: cache hit for %r", raw)
        return cached

    # ── Step 3: venue match ─────────────────────────────────────────────────────
    venue_resolved = venues.match(raw)
    if venue_resolved is not None:
        log.debug("resolver: venue match for %r", raw)
        # Copy with updated source to indicate we went through cache
        result = ResolvedLocation(
            kind=venue_resolved.kind,
            value=venue_resolved.value,
            lat=venue_resolved.lat,
            lon=venue_resolved.lon,
            source="known_venues",
        )
        store.cache_geocode(raw, result)
        return result

    # ── Step 4: address heuristic → geocode ─────────────────────────────────────
    if looks_like_address(raw):
        geo_result = geocoder(raw)
        if geo_result is not None:
            log.debug("resolver: geocoded %r via heuristic", raw)
            result = ResolvedLocation(
                kind="address",
                value=geo_result.formatted_address,
                lat=geo_result.lat,
                lon=geo_result.lon,
                source="geocode",
            )
            store.cache_geocode(raw, result)
            return result

    # ── Step 4b: placeholder check ───────────────────────────────────────────────
    if _is_placeholder(raw):
        log.debug("resolver: placeholder %r skipped", raw)
        return None

    # ── Step 5: LLM resolution ─────────────────────────────────────────────────
    llm_resolved = llm.resolve_location(raw, hints={})
    if llm_resolved is not None:
        if llm_resolved.kind == "station":
            # Stations don't need geocoding — cache as-is and return
            log.debug("resolver: LLM station match for %r", raw)
            store.cache_geocode(raw, llm_resolved)
            return llm_resolved

        if llm_resolved.kind == "address":
            # Geocode the address returned by LLM, then cache
            geo_result = geocoder(llm_resolved.value)
            if geo_result is not None:
                log.debug("resolver: LLM address geocoded for %r", raw)
                result = ResolvedLocation(
                    kind="address",
                    value=geo_result.formatted_address,
                    lat=geo_result.lat,
                    lon=geo_result.lon,
                    source="llm",
                )
                store.cache_geocode(raw, result)
                return result

    # ── Step 6: unresolved ───────────────────────────────────────────────────────
    log.warning("unresolved_location raw=%r", raw)
    return None
