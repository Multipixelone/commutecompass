"""Integration tests for resolver.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from commutecompass.geocode import GeocodeResult
from commutecompass.models import ResolvedLocation, ZoneInfo
from commutecompass.resolver import looks_like_address, resolve
from commutecompass.venues import VenueEntry, VenueRegistry


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_store() -> MagicMock:
    """A mock Store with no cached entries by default."""
    store = MagicMock()
    store.get_geocode.return_value = None
    return store


@pytest.fixture
def mock_geocoder() -> MagicMock:
    """A mock geocoder that returns None by default."""
    geocoder = MagicMock()
    geocoder.return_value = None
    return geocoder


@pytest.fixture
def mock_llm() -> MagicMock:
    """A mock LLM client that returns None by default."""
    llm = MagicMock()
    llm.resolve_location.return_value = None
    return llm


@pytest.fixture
def venue_registry() -> VenueRegistry:
    """A pre-loaded venue registry with known entries."""
    entries = [
        VenueEntry(
            aliases=["200 Example St", "Example School", "Studio 100"],
            resolves_to=ResolvedLocation(
                kind="address",
                value="200 Example St, New York, NY 10001",
                source="known_venues",
            ),
        ),
        VenueEntry(
            aliases=["Example University", "Example Theater"],
            resolves_to=ResolvedLocation(
                kind="station",
                value="Example LIRR Station, NY",
                source="known_venues",
            ),
        ),
    ]
    return VenueRegistry(entries=entries)


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestLooksLikeAddress:
    """Unit tests for the looks_like_address heuristic."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("200 Example St, New York, NY", True),
            ("123 Main St", True),
            ("456 Park Ave", True),
            ("789 Oak Boulevard", True),
            ("Lincoln Center", False),          # no number
            ("Broadway", False),                 # no number
            ("", False),                         # empty
            ("Theatre District", False),        # no number
            ("Manhattan", False),               # no number
        ],
    )
    def test_looks_like_address(self, raw: str, expected: bool) -> None:
        assert looks_like_address(raw) == expected


class TestIsPlaceholder:
    """Unit tests for the placeholder heuristic."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Location available once RSVP'd", True),
            ("Location Available Once RSVP'd", True),  # different case
            ("Location revealed after RSVP", True),
            ("RSVP to see location", True),
            ("TBA", True),
            ("TBD", True),
            ("TBD Location", True),
            ("200 Example St", False),  # real address
            ("Lincoln Center", False),  # venue name
            ("Home", False),  # plain word
            ("", False),  # empty
        ],
    )
    def test_is_placeholder(self, raw: str, expected: bool) -> None:
        from commutecompass.resolver import _is_placeholder

        assert _is_placeholder(raw) == expected


class TestResolve:
    """Integration tests for the full resolution pipeline."""

    # ── Step 1: empty raw ───────────────────────────────────────────────────────

    def test_empty_none_returns_none(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            None,
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )
        assert result is None

    def test_empty_whitespace_returns_none(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "   ",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )
        assert result is None

    # ── Step 4b: placeholder ───────────────────────────────────────────────────

    def test_placeholder_rsvp_returns_none_and_skips_llm_geocoder(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        """A clearly placeholder location like 'Location available once RSVP'd' returns None and does NOT call LLM or geocoder."""
        result = resolve(
            "Location available once RSVP'd",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is None
        # Neither LLM nor geocoder should be called for placeholder strings
        mock_llm.resolve_location.assert_not_called()
        mock_geocoder.assert_not_called()
        # Nothing should be cached either
        mock_store.cache_geocode.assert_not_called()

    # ── Step 2: cache hit ───────────────────────────────────────────────────────

    def test_cache_hit(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        cached_location = ResolvedLocation(
            kind="address",
            value="1 Infinite Loop, Cupertino, CA",
            lat=37.3861,
            lon=-122.0600,
            source="cache",
        )
        mock_store.get_geocode.return_value = cached_location

        result = resolve(
            "Apple HQ",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is cached_location
        mock_store.get_geocode.assert_called_once_with("Apple HQ")
        # Should NOT call geocoder or LLM
        mock_geocoder.assert_not_called()
        mock_llm.resolve_location.assert_not_called()

    # ── Step 3: venue match ─────────────────────────────────────────────────────

    def test_venue_match_exact_alias(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "Example School",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is not None
        assert result.kind == "address"
        assert result.value == "200 Example St, New York, NY 10001"
        assert result.source == "known_venues"
        # Should cache the result
        mock_store.cache_geocode.assert_called_once()
        # Should NOT call geocoder or LLM
        mock_geocoder.assert_not_called()
        mock_llm.resolve_location.assert_not_called()

    def test_venue_match_station(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "Example University",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is not None
        assert result.kind == "station"
        assert result.value == "Example LIRR Station, NY"
        assert result.source == "known_venues"
        mock_store.cache_geocode.assert_called_once()
        mock_geocoder.assert_not_called()
        mock_llm.resolve_location.assert_not_called()

    # ── Step 4: looks_like_address → geocode ────────────────────────────────────

    def test_address_heuristic_geocodes(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        geo_result = GeocodeResult(
            formatted_address="350 Fifth Ave, New York, NY 10118",
            lat=40.7484,
            lon=-73.9857,
            place_id="ChIJIQ1p3_2pXokRNrMfXBFb4Xs",
        )
        mock_geocoder.return_value = geo_result

        result = resolve(
            "350 Fifth Ave",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is not None
        assert result.kind == "address"
        assert result.value == "350 Fifth Ave, New York, NY 10118"
        assert result.lat == 40.7484
        assert result.lon == -73.9857
        assert result.source == "geocode"
        mock_geocoder.assert_called_once_with("350 Fifth Ave")
        mock_store.cache_geocode.assert_called_once()
        mock_llm.resolve_location.assert_not_called()

    def test_address_heuristic_geocode_returns_none_does_not_cache(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        # Geocoder fails -> falls through to LLM
        mock_geocoder.return_value = None

        resolve(
            "789 Unknown Street",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        # Falls through to LLM, but LLM also returns None -> total miss
        # (verify call chain, cache not called)
        mock_llm.resolve_location.assert_called_once()
        mock_store.cache_geocode.assert_not_called()

    # ── Step 5: LLM resolution ─────────────────────────────────────────────────

    def test_llm_station_caches_and_returns(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        mock_llm.resolve_location.return_value = ResolvedLocation(
            kind="station",
            value="Jamaica LIRR Station, NY",
            source="llm",
        )

        result = resolve(
            "LIRR to Jamaica",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is not None
        assert result.kind == "station"
        assert result.value == "Jamaica LIRR Station, NY"
        assert result.source == "llm"
        # Station: cached but NOT geocoded
        mock_store.cache_geocode.assert_called_once()
        mock_geocoder.assert_not_called()

    def test_llm_address_geocodes_and_caches(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        mock_llm.resolve_location.return_value = ResolvedLocation(
            kind="address",
            value="200 W 44th St, New York, NY",
            source="llm",
        )
        geo_result = GeocodeResult(
            formatted_address="200 W 44th St, New York, NY 10036",
            lat=40.7579,
            lon=-73.9875,
            place_id="ChIJYzNn1xPWXokRj9-8ZBGQFGU",
        )
        mock_geocoder.return_value = geo_result

        result = resolve(
            "Broadway show location",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is not None
        assert result.kind == "address"
        assert result.value == "200 W 44th St, New York, NY 10036"
        assert result.lat == 40.7579
        assert result.lon == -73.9875
        assert result.source == "llm"
        # LLM returns address -> geocoder is called with the LLM value
        mock_geocoder.assert_called_once_with("200 W 44th St, New York, NY")
        mock_store.cache_geocode.assert_called_once()

    def test_llm_returns_unknown(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        mock_llm.resolve_location.return_value = None  # kind=="unknown" -> None

        result = resolve(
            "some ambiguous place",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is None
        mock_geocoder.assert_not_called()
        mock_store.cache_geocode.assert_not_called()

    # ── Step 6: total miss ───────────────────────────────────────────────────────

    def test_total_miss_returns_none(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "Some completely ambiguous location xyz123",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )

        assert result is None
        mock_store.cache_geocode.assert_not_called()


# ── HA zone short-circuit ─────────────────────────────────────────────────────


class TestHaZoneMatch:
    def _zones(self) -> dict[str, ZoneInfo]:
        return {
            "work": ZoneInfo(
                name="Work",
                lat=40.7346,
                lon=-74.0055,
                radius_m=128.0,
                entity_id="zone.work",
            ),
            "madison theatre": ZoneInfo(
                name="Madison Theatre",
                lat=40.6862,
                lon=-73.6266,
                radius_m=172.0,
                entity_id="zone.madison_theatre",
            ),
        }

    def test_zone_match_short_circuits_before_cache(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "Madison Theatre",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
            ha_zones=self._zones(),
        )

        assert result is not None
        assert result.source == "ha_zone"
        assert result.lat == 40.6862
        assert result.value == "Madison Theatre"
        # Cache neither read nor written so HA edits propagate next call.
        mock_store.get_geocode.assert_not_called()
        mock_store.cache_geocode.assert_not_called()
        mock_geocoder.assert_not_called()

    def test_zone_match_is_case_insensitive(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "  work  ",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
            ha_zones=self._zones(),
        )
        assert result is not None
        assert result.source == "ha_zone"

    def test_no_partial_match(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        """'Office Tower' must NOT match a 'office' zone — exact (lowercased) only."""
        from commutecompass.models import ZoneInfo

        zones = {
            "office": ZoneInfo(
                name="Office",
                lat=40.0,
                lon=-74.0,
                entity_id="zone.office",
            ),
        }
        mock_llm.resolve_location.return_value = None
        result = resolve(
            "Office Tower",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
            ha_zones=zones,
        )
        # Falls through to cache check and downstream — no zone short-circuit.
        mock_store.get_geocode.assert_called_once()
        assert result is None or result.source != "ha_zone"

    def test_no_zones_arg_keeps_existing_behavior(
        self,
        mock_store: MagicMock,
        mock_geocoder: MagicMock,
        mock_llm: MagicMock,
        venue_registry: VenueRegistry,
    ) -> None:
        result = resolve(
            "200 Example St",
            venues=venue_registry,
            store=mock_store,
            geocoder=mock_geocoder,
            llm=mock_llm,
        )
        # Hits venue registry as before.
        assert result is not None
        assert result.source == "known_venues"
