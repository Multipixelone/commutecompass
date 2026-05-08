"""Plan-an-event orchestrator."""

from __future__ import annotations

from commutecop.models import Config, Event, Plan
from commutecop.venues import VenueRegistry
from commutecop.llm import OpencodeGoClient


def plan_event(
    event: Event,
    config: Config,
    venues: VenueRegistry,
    store: "Store",  # type: ignore[name-defined]
    llm: OpencodeGoClient,
    *,
    mode_override: Optional[str] = None,
) -> Plan:
    """Compute optimal departure time for an event.

    Returns a Plan with route and timing, or an error Plan on failure.
    """
    raise NotImplementedError()