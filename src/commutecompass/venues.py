"""Venue registry for known locations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
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


def _jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    if union == 0:
        return 0.0
    return intersection / union


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
        # Collapsed means whitespace removed so "CAP    21" and "Example School" both → "school".
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
        3. Fuzzy token-overlap match (Jaccard >= 0.85 on whitespace-collapsed alias vs input) → return resolution
        4. Otherwise None
        """
        if not raw:
            return None

        norm = _normalize(raw)

        # Step 1: exact match
        if norm in self._exact:
            idx = self._exact[norm]
            return self.entries[idx].resolves_to

        # Step 2: fuzzy — compare whitespace-collapsed input against stored collapsed aliases
        collapsed_input = re.sub(r"\s+", "", norm)
        for stored_collapsed, idx in self._fuzzy:
            # Jaccard over character bigrams (or fallback to simple overlap ratio)
            # Simple ratio: number of shared tokens / total tokens
            # Build token sets by splitting on whitespace after collapsing
            input_tokens = set(collapsed_input)
            stored_tokens = set(stored_collapsed)
            if _jaccard(input_tokens, stored_tokens) >= 0.85:
                return self.entries[idx].resolves_to

        return None