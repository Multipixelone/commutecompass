"""Tests for venues.py."""

from __future__ import annotations

from pathlib import Path


from commutecompass.models import ResolvedLocation
from commutecompass.venues import VenueRegistry, VenueEntry


FIXTURE_PATH = Path(__file__).parent.parent / "data" / "known_venues.yaml"


def test_exact_match_school() -> None:
    """Example School matches exactly as an alias."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example School")
    assert result is not None
    assert result.value == "200 Example St, New York, NY 10001"
    assert result.source == "known_venues"


def test_exact_match_404() -> None:
    """404 matches exactly as an alias."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("404")
    assert result is not None
    assert result.value == "200 Example St, New York, NY 10001"


def test_exact_match_university() -> None:
    """Example University matches exactly."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example University")
    assert result is not None
    assert result.kind == "station"
    assert result.value == "Example LIRR Station, NY"


def test_fuzzy_match_school_lowercase() -> None:
    """Fuzzy: 'Example School' (case-insensitive) matches via token Jaccard."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example School")
    assert result is not None
    assert result.value == "200 Example St, New York, NY 10001"


def test_fuzzy_match_studio_404_typo() -> None:
    """Fuzzy: 'Studio 100' with extra space still matches."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Studio 100")
    assert result is not None
    assert result.value == "200 Example St, New York, NY 10001"


def test_fuzzy_match_example() -> None:
    """Fuzzy: 'Example' alone matches via Jaccard threshold."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example")
    assert result is not None
    assert result.kind == "station"
    assert result.value == "Example LIRR Station, NY"


def test_fuzzy_match_theater() -> None:
    """Fuzzy: 'Example Theater' matches via token overlap."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example Theater")
    assert result is not None
    assert result.kind == "station"


def test_fuzzy_match_collapsed_whitespace_variant() -> None:
    """'Studio100' (no space) fuzzy-matches the 'Studio 100' alias."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Studio100")
    assert result is not None
    assert result.value == "200 Example St, New York, NY 10001"


def test_fuzzy_does_not_match_different_room_number() -> None:
    """'Studio 200' must NOT match 'Studio 100' — different rooms (regression)."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    assert registry.match("Studio 200") is None
    assert registry.match("Studio200") is None


def test_fuzzy_does_not_match_anagram() -> None:
    """A character anagram must not match (the old char-set Jaccard bug)."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    # "loohcs elpmaxe" has the same characters as "example school" reversed.
    assert registry.match("loohcs elpmaxe") is None


def test_no_match_returns_none() -> None:
    """Unknown venue returns None."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Some Completely Unknown Place")
    assert result is None


def test_empty_string_returns_none() -> None:
    """Empty input returns None."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("")
    assert result is None


def test_venue_registry_load() -> None:
    """VenueRegistry.load reads valid YAML and constructs entries."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    assert len(registry.entries) == 2
    assert registry.entries[0].aliases[0] == "200 Example St"


def test_venue_entry_model() -> None:
    """VenueEntry constructs correctly with ResolvedLocation."""
    entry = VenueEntry(
        aliases=["Test Place", "Test"],
        resolves_to=ResolvedLocation(
            kind="address",
            value="123 Main St",
            source="known_venues",
        ),
    )
    assert entry.aliases == ["Test Place", "Test"]
    assert entry.resolves_to.value == "123 Main St"


def test_punctuation_stripped_for_exact_match() -> None:
    """Punctuation in input is stripped before matching."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example School!!")
    assert result is not None


def test_whitespace_collapsed_for_fuzzy_match() -> None:
    """Multiple spaces are collapsed before fuzzy matching."""
    registry = VenueRegistry.load(FIXTURE_PATH)
    result = registry.match("Example    School")
    assert result is not None