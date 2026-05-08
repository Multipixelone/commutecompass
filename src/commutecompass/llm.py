"""opencode-go LLM client for location resolution."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from commutecompass.models import ResolvedLocation

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You resolve calendar event location strings to either a geocodable street
address in NYC or a transit station name. The user lives in Brooklyn and
attends classes/rehearsals in Manhattan and on Long Island.

Return ONLY a JSON object with shape:
  {"kind": "address" | "station" | "unknown", "value": "..."}

If you can map the raw string to a known NYC venue, prefer "address".
If the destination is on Long Island and clearly served by an LIRR station,
return "station" with the station name in the format "<Town> LIRR Station, NY".
If you cannot resolve it confidently, return {"kind": "unknown", "value": ""}.
"""


class OpencodeGoClient:
    """Client for the opencode-go API (OpenAI-compatible chat completions)."""

    def __init__(self, endpoint: str, token: str, model: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self.model = model

    def resolve_location(self, raw: str, hints: dict) -> Optional[ResolvedLocation]:
        """Resolve a raw location string to a ResolvedLocation using the LLM.

        Returns None on failure, parse error, timeout, or kind=="unknown".
        """
        try:
            response = self._call(raw, hints)
        except httpx.TimeoutException:
            log.warning("opencode-go request timed out for raw=%r", raw)
            return None
        except Exception as exc:
            log.warning("opencode-go request failed for raw=%r: %s", raw, exc)
            return None

        location = self._parse_response(response)
        return location

    def _call(self, raw: str, hints: dict) -> str:
        """Make the chat completion request and return the content string."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": raw},
            ],
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # OpenAI-compatible chat completion shape
        return data["choices"][0]["message"]["content"]

    def _parse_response(self, content: str) -> Optional[ResolvedLocation]:
        """Parse JSON from the response content, handling fenced JSON."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", content.strip()).strip()
        # Remove any trailing fence markers
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.warning("opencode-go JSON parse failed: %s — content: %r", exc, content)
            return None

        kind = parsed.get("kind")
        if kind == "unknown":
            return None

        if kind not in ("address", "station"):
            log.warning("opencode-go returned unknown kind=%r, treating as None", kind)
            return None

        value = parsed.get("value", "")
        if not value:
            return None

        return ResolvedLocation(
            kind=kind,
            value=value,
            source="llm",
        )