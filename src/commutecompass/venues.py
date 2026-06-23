"""Venue registry for known locations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from commutecompass.models import ResolvedLocation


def _normalize(s: str) -> str:
    """Normalize a string for comparison: lowercase, strip punctuation, collapse whitespace."""
    # Lowercase
    s = s.lower()
    # Strip punctuation (keep alphanumeric and spaces)
    s = re.sub(r"[^\w\s]", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# rapidfuzz ratios run 0-100; require a strong overlap before claiming a match.
_FUZZY_THRESHOLD = 85.0


def _digit_runs(s: str) -> set[str]:
    """Extract digit sequences (e.g. room/studio numbers) from a string.

    Numbers carry meaning that edit-distance smears over: 'studio 100' and
    'studio 200' are 90% similar as strings but are different rooms.  Requiring
    digit runs to match exactly before a fuzzy comparison keeps those apart.
    """
    return set(re.findall(r"\d+", s))


class VenueEntry(BaseModel):
    """A known venue with aliases and resolution."""
    aliases: list[str]
    resolves_to: ResolvedLocation


class VenueRegistry:
    """Registry of known venues loaded from YAML."""

    def __init__(self, entries: list[VenueEntry]) -> None:
        self.entries = entries
        # Build normalized alias -> entry index mapping for fast exact lookup
        self._exact: dict[str, int] = {}
        # Build list of (collapsed_alias, entry_index) for fuzzy matching.
        # Collapsed means whitespace removed so "Studio  100" and "Studio100" both → "studio100".
        self._fuzzy: list[tuple[str, int]] = []
        for idx, entry in enumerate(entries):
            for alias in entry.aliases:
                norm = _normalize(alias)
                self._exact[norm] = idx
            for alias in entry.aliases:
                collapsed = re.sub(r"\s+", "", _normalize(alias))
                if collapsed:
                    self._fuzzy.append((collapsed, idx))

    @classmethod
    def load(cls, path: Path) -> "VenueRegistry":
        """Load venue entries from a YAML file."""
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        entries = []
        for item in raw:
            # Inject source since YAML doesn't include it (always known_venues)
            item["resolves_to"]["source"] = "known_venues"
            entries.append(VenueEntry.model_validate(item))
        return cls(entries=entries)

    def match(self, raw: str) -> Optional[ResolvedLocation]:
        """Match a raw location string against known aliases.

        Matching strategy:
        1. Normalize input
        2. Exact alias match (normalized) → return resolution
        3. Fuzzy match: edit-distance ratio >= threshold on the whitespace-
           collapsed strings (so "Studio 100" and "Studio100" match), but only
           when both sides have the *same* digit runs — so different room
           numbers ("Studio 100" vs "Studio 200") never collide.
        4. Otherwise None
        """
        if not raw:
            return None

        from rapidfuzz import fuzz

        norm = _normalize(raw)

        # Step 1: exact match
        if norm in self._exact:
            idx = self._exact[norm]
            return self.entries[idx].resolves_to

        # Step 2: fuzzy — edit-distance over whitespace-collapsed strings, gated
        # on matching digit runs.  Unlike the previous character-set Jaccard this
        # respects order (anagrams no longer match) and room numbers.
        collapsed_input = re.sub(r"\s+", "", norm)
        input_digits = _digit_runs(collapsed_input)
        for stored_collapsed, idx in self._fuzzy:
            if _digit_runs(stored_collapsed) != input_digits:
                continue
            if fuzz.ratio(collapsed_input, stored_collapsed) >= _FUZZY_THRESHOLD:
                return self.entries[idx].resolves_to

        return None
