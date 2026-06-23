"""Dead-man's-switch heartbeat.

A self-hosted alarm has a silent failure mode: if the per-minute poll timer
stops firing, the user just stops getting notifications and has no way to know.
The internal `job_heartbeat` table lets the morning digest report that poll went
stale, and an optional external healthchecks.io-style URL provides an off-host
safety net that alerts when the pings stop entirely.
"""

from __future__ import annotations

import logging

import httpx

from commutecompass.retry import retry

logger = logging.getLogger(__name__)


def ping_heartbeat(url: str, *, timeout: float = 5.0) -> bool:
    """GET a healthcheck URL to signal liveness.  Returns True on 2xx.

    Failures are swallowed (logged at debug): a monitoring blip must never break
    the job whose health it is reporting.
    """
    if not url:
        return False

    def _do() -> None:
        with httpx.Client(timeout=timeout) as client:
            client.get(url).raise_for_status()

    try:
        retry(_do, attempts=2, label="heartbeat")
        return True
    except Exception as exc:  # pragma: no cover - exercised via swallow path
        logger.debug("heartbeat ping failed for %s: %s", url, exc)
        return False
