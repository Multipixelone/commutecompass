"""Small retry helper for transient HTTP failures.

External calls (Google geocode, opencode-go LLM, MTA GTFS-RT, Home Assistant)
all benefit from "retry once or twice on a 5xx / timeout, give up on 4xx".
A bespoke helper keeps the policy in one place — easier to tune than every
caller picking its own backoff curve.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import httpx


T = TypeVar("T")

_logger = logging.getLogger(__name__)


def is_transient_http_error(exc: BaseException) -> bool:
    """Return True for the failure shapes worth retrying.

    Retry: network errors, timeouts, 5xx responses, 429.
    Do NOT retry: 4xx (except 429) — those reflect a request bug, not a
    transient condition, and retrying just wastes API budget.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    label: str = "operation",
) -> T:
    """Call ``fn`` up to ``attempts`` times with exponential backoff + jitter.

    Re-raises the last exception once attempts are exhausted.  Non-transient
    exceptions are raised immediately (no retry).

    Args:
        fn: zero-arg callable to invoke.
        attempts: total tries including the first (must be >= 1).
        base_delay: first sleep duration (seconds).  Subsequent attempts back
            off as ``base_delay * 2**n`` capped by ``max_delay``.
        max_delay: cap on the sleep between attempts.
        label: identifier for log messages.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if not is_transient_http_error(exc):
                raise
            if attempt == attempts - 1:
                break
            sleep = min(base_delay * (2 ** attempt), max_delay)
            sleep += random.uniform(0, sleep * 0.2)  # +0–20% jitter
            _logger.info(
                "%s: transient error %s — retry %d/%d after %.2fs",
                label,
                exc.__class__.__name__,
                attempt + 2,
                attempts,
                sleep,
            )
            time.sleep(sleep)
    assert last_exc is not None
    raise last_exc
