"""Tests for jobs."""

from __future__ import annotations

import pytest

from commutecop.jobs import morning_run, poll_run


def test_morning_run_stub() -> None:
    """Stub: morning_run raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        morning_run(None)  # type: ignore[arg-type]


def test_poll_run_stub() -> None:
    """Stub: poll_run raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        poll_run(None)  # type: ignore[arg-type]