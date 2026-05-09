"""opencode-go LLM client for location resolution and alert relevance."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

import httpx

from commutecompass.models import ResolvedLocation

if TYPE_CHECKING:
    from commutecompass.models import Alert, Route

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

_ALERT_RELEVANCE_PROMPT = """You classify whether an MTA service alert is actionable for a specific commute route/time.

Return ONLY JSON with shape:
  {"relevant": true | false | null, "reason": "short reason"}

Guidance:
- relevant=true for disruptions likely to change trip timing/routing (delays, suspended/no service, reroutes, skip-stop, major planned work) that impact the route/time.
- relevant=false for advisories that usually do not require replanning (elevator/escalator outages, station booth/parking notices, unrelated systems).
- relevant=null only when genuinely ambiguous.
- Be strict; prefer false over true when weak evidence.
"""


class OpencodeGoClient:
    """Client for the opencode-go API (OpenAI-compatible chat completions)."""

    def __init__(self, endpoint: str, token: str, model: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self.model = model

    def resolve_location(self, raw: str, hints: dict[str, Any]) -> Optional[ResolvedLocation]:
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

    def _call(self, raw: str, hints: dict[str, Any]) -> str:
        """Make the chat completion request and return the content string."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": raw},
            ],
            "temperature": 0.0,
        }
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            return ""
        # OpenAI-compatible chat completion shape
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

    def _chat_completion(self, system_prompt: str, user_content: str, *, timeout_seconds: float = 8.0) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
        }
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

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

    def classify_alert_relevance(
        self,
        alert: "Alert",
        route: "Route",
        *,
        at_time: datetime,
    ) -> Optional[bool]:
        """Classify alert relevance for a specific route/time.

        Returns True/False when model is confident, None otherwise.
        """
        user_payload = {
            "at_time": at_time.isoformat(),
            "alert": {
                "id": alert.id,
                "header": alert.header,
                "description": alert.description,
                "severity": alert.severity,
                "affected_routes": sorted(alert.affected_routes),
                "affected_systems": sorted(alert.affected_systems),
                "active_periods": [
                    [
                        start.isoformat() if start else None,
                        end.isoformat() if end else None,
                    ]
                    for start, end in alert.active_periods
                ],
            },
            "route": {
                "depart_at": route.depart_at.isoformat(),
                "arrive_at": route.arrive_at.isoformat(),
                "legs": [
                    {
                        "mode": leg.mode,
                        "system": leg.system,
                        "line": leg.line,
                        "depart_at": leg.depart_at.isoformat(),
                        "arrive_at": leg.arrive_at.isoformat(),
                    }
                    for leg in route.legs
                ],
            },
        }

        try:
            content = self._chat_completion(
                _ALERT_RELEVANCE_PROMPT,
                json.dumps(user_payload, ensure_ascii=False),
                timeout_seconds=6.0,
            )
        except httpx.TimeoutException:
            log.debug("alert relevance LLM timeout alert_id=%s", alert.id)
            return None
        except Exception as exc:
            log.debug("alert relevance LLM failed alert_id=%s: %s", alert.id, exc)
            return None

        cleaned = re.sub(r"```(?:json)?\s*", "", content.strip()).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            log.debug("alert relevance parse failure alert_id=%s", alert.id)
            return None

        relevant = parsed.get("relevant")
        if isinstance(relevant, bool):
            return relevant
        return None
