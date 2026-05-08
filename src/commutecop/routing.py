"""Google Directions routing."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from commutecop.models import Origin, ResolvedLocation, Route


def plan_route(
    origin: Origin,
    destination: ResolvedLocation,
    arrival_time: datetime,
    mode: Literal["transit", "driving", "walking", "bicycling"] = "transit",
    api_key: str = "",
) -> Optional[Route]:
    """Plan a route from origin to destination.

    Returns None if no route found.
    """
    raise NotImplementedError()