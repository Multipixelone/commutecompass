"""Tests for mta.py."""

from __future__ import annotations

import pytest

from commutecop.mta import fetch_alerts, alerts_affecting_route


def test_fetch_alerts_stub() -> None:
    """Stub: fetch_alerts raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        fetch_alerts("", "", "")


def test_alerts_affecting_route_stub() -> None:
    """Stub: alerts_affecting_route raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        alerts_affecting_route([], None, None)  # type: ignore[arg-type]