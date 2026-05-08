"""opencode-go LLM client for location resolution."""

from __future__ import annotations

from commutecop.models import ResolvedLocation


class OpencodeGoClient:
    """Client for the opencode-go API."""

    def __init__(self, endpoint: str, token: str, model: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self.model = model

    def resolve_location(self, raw: str, hints: dict) -> Optional[ResolvedLocation]:
        """Resolve a raw location string to a ResolvedLocation using the LLM.

        Returns None on failure or unknown.
        """
        raise NotImplementedError()