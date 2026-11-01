"""Instant-based comparison helpers for DST-safe test assertions.

Per PEP 495, ``==`` between an aware ``ZoneInfo`` datetime inside the NYC
fall-back hour and any datetime with a different ``tzinfo`` (e.g. the
fixed-offset value produced by a JSON/ISO round-trip) is always ``False``.
Compare UTC instants instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from commutecompass.models import Plan
from commutecompass.store import _same_plan


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as a UTC instant (``None`` passes through)."""
    return None if dt is None else dt.astimezone(UTC)


def same_plan(a: Optional[Plan], b: Optional[Plan]) -> bool:
    """Plan equality with every datetime compared as a UTC instant."""
    if a is None or b is None:
        return a is b
    return _same_plan(a, b)
