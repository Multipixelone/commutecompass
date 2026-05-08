"""MTA GTFS-RT alert fetcher and matcher."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from commutecop.models import Alert, Route


def fetch_alerts(
    subway_url: str,
    lirr_url: str,
    bus_url: str,
) -> list[Alert]:
    """Fetch and parse MTA GTFS-RT alert feeds from all sources."""
    raise NotImplementedError()


def alerts_affecting_route(
    alerts: list[Alert],
    route: Route,
    at_time: datetime,
) -> list[Alert]:
    """Return alerts from the list that affect the given route at the given time."""
    raise NotImplementedError()