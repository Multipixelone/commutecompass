"""Tests for format.py."""

from __future__ import annotations

import pytest

from commutecop.format import escape_md, format_digest, format_prep_ping, format_leave_ping, format_service_update


def test_escape_md_basic() -> None:
    """Stub: escape_md escapes Telegram special characters."""
    result = escape_md("test_string")
    assert result == "test\\_string"


def test_escape_md_complex() -> None:
    """Stub: escape_md handles multiple special chars."""
    result = escape_md("*bold* and _italic_")
    assert r"\*bold\*" in result
    assert r"\_italic\_" in result


def test_format_digest_stub() -> None:
    """Stub: format_digest raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        format_digest([], [])


def test_format_prep_ping_stub() -> None:
    """Stub: format_prep_ping raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        format_prep_ping(None)  # type: ignore[arg-type]


def test_format_leave_ping_stub() -> None:
    """Stub: format_leave_ping raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        format_leave_ping(None)  # type: ignore[arg-type]


def test_format_service_update_stub() -> None:
    """Stub: format_service_update raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        format_service_update(None, None, None)  # type: ignore[arg-type]