"""Tests for resolver.py."""

from __future__ import annotations

import pytest

from commutecop.resolver import resolve


def test_resolve_stub() -> None:
    """Stub: resolve raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        resolve(None, venues=None, store=None, geocoder=None, llm=None)  # type: ignore[arg-type]