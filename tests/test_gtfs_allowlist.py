"""Tests for the vendored GTFS bridge allowlist and its strict loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from commutecompass.gtfs_allowlist import (
    AllowlistError,
    haversine_m,
    load_allowlist,
    normalize_line,
    normalize_name,
    parse_allowlist,
)

_GOOD_ENTRY = """
[[platform]]
route_ids = ["A", "C"]
directions_line = ["A", "C"]
parent_station = "A48"
stop_name = "Utica Av"
stop_lat = 40.679364
stop_lon = -73.930729
platforms = { N = "A48N", S = "A48S" }
directions_names = ["Utica Av"]
max_coord_distance_m = 150
"""


def _doc(entry: str = _GOOD_ENTRY, header: str = "schema_version = 1\n") -> str:
    return header + entry


class TestBundledAllowlist:
    def test_bundled_stage1_set(self) -> None:
        entries = load_allowlist()
        by_parent = {e.parent_station: e for e in entries}
        assert set(by_parent) == {"A48", "A32", "D15"}
        assert by_parent["A48"].route_ids == ("A", "C")
        assert by_parent["A32"].route_ids == ("A", "C")
        assert by_parent["D15"].route_ids == ("B", "D", "F", "M")
        assert by_parent["A32"].stop_name == "W 4 St-Wash Sq"
        assert by_parent["D15"].stop_name == "47-50 Sts-Rockefeller Ctr"
        for e in entries:
            assert e.platform_ids == (e.parent_station + "N", e.parent_station + "S")

    def test_w4_lower_level_not_listed(self) -> None:
        assert all(e.parent_station != "D20" for e in load_allowlist())

    def test_no_express_variants_implicitly(self) -> None:
        for e in load_allowlist():
            assert "FX" not in e.route_ids


class TestStrictValidation:
    def test_good_document_parses(self) -> None:
        (entry,) = parse_allowlist(_doc())
        assert entry.platforms == {"N": "A48N", "S": "A48S"}

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(AllowlistError):
            load_allowlist(tmp_path / "nope.toml")

    def test_bad_toml(self) -> None:
        with pytest.raises(AllowlistError, match="invalid TOML"):
            parse_allowlist("[[platform]\n")

    def test_wrong_schema_version(self) -> None:
        with pytest.raises(AllowlistError, match="schema_version"):
            parse_allowlist(_doc(header="schema_version = 2\n"))

    def test_no_entries(self) -> None:
        with pytest.raises(AllowlistError, match="at least one"):
            parse_allowlist("schema_version = 1\n")

    def test_unknown_key(self) -> None:
        with pytest.raises(AllowlistError, match="unknown keys"):
            parse_allowlist(_doc(_GOOD_ENTRY + 'extra = "x"\n'))

    def test_unknown_top_key(self) -> None:
        with pytest.raises(AllowlistError, match="top-level"):
            parse_allowlist(_doc(header='schema_version = 1\nfoo = 1\n'))

    def test_missing_key(self) -> None:
        with pytest.raises(AllowlistError, match="missing keys"):
            parse_allowlist(_doc(_GOOD_ENTRY.replace('stop_name = "Utica Av"\n', "")))

    @pytest.mark.parametrize(
        ("old", "new", "match"),
        [
            ('platforms = { N = "A48N", S = "A48S" }', 'platforms = { N = "A48N" }', "N and S"),
            ('S = "A48S"', 'S = "A47S"', "is not"),
            ("stop_lat = 40.679364", "stop_lat = -73.93", "outside NYC"),
            ("stop_lat = 40.679364", 'stop_lat = "40.6"', "number"),
            ("max_coord_distance_m = 150", "max_coord_distance_m = 0", "max_coord"),
            ('route_ids = ["A", "C"]', "route_ids = []", "non-empty"),
            ('route_ids = ["A", "C"]', 'route_ids = ["A", "A"]', "duplicates"),
            ('route_ids = ["A", "C"]', 'route_ids = ["A-1"]', "alphanumeric"),
            ('directions_line = ["A", "C"]', 'directions_line = ["a"]', "pre-normalized"),
            ('parent_station = "A48"', "parent_station = 48", "alphanumeric"),
        ],
    )
    def test_field_rejections(self, old: str, new: str, match: str) -> None:
        assert old in _GOOD_ENTRY
        with pytest.raises(AllowlistError, match=match):
            parse_allowlist(_doc(_GOOD_ENTRY.replace(old, new)))

    def test_duplicate_parent(self) -> None:
        with pytest.raises(AllowlistError, match="duplicate parent"):
            parse_allowlist(_doc(_GOOD_ENTRY + _GOOD_ENTRY))


class TestHelpers:
    def test_normalize_line(self) -> None:
        assert normalize_line(" 6x ") == "6X"
        assert normalize_line("6X") != normalize_line("6")

    def test_normalize_name(self) -> None:
        assert normalize_name("  Utica   AV ") == "utica av"

    def test_haversine(self) -> None:
        assert haversine_m(40.0, -74.0, 40.0, -74.0) == 0.0
        # 0.001 deg latitude ≈ 111 m
        assert 110 < haversine_m(40.0, -74.0, 40.001, -74.0) < 112
