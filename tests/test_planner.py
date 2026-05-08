"""Tests for planner.py."""

from __future__ import annotations

import pytest

from commutecop.planner import plan_event


def test_plan_event_stub() -> None:
    """Stub: plan_event raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        plan_event(None, None, None, None, None)  # type: ignore[arg-type]