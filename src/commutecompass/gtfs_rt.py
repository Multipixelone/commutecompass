"""Shared GTFS-RT feed fetching.

Both the alert matcher (:mod:`commutecompass.mta`) and the real-time departure
buffer (:mod:`commutecompass.realtime`) read MTA GTFS-RT protobuf feeds.  The
fetch-and-validate step is identical — and the validation matters: MTA feeds
occasionally answer with an XML/JSON error envelope or an empty body instead of
protobuf, and ``FeedMessage.FromString`` on that garbage raises an opaque
``DecodeError``.  Keeping the diagnostics in one place means both callers get the
same actionable error message.
"""

from __future__ import annotations

import httpx
from google.transit.gtfs_realtime_pb2 import (  # type: ignore[import-untyped]
    FeedMessage,
)

__all__ = ["fetch_feed_message"]


def fetch_feed_message(url: str, system: str, client: httpx.Client) -> FeedMessage:
    """Fetch a single GTFS-RT feed and parse it into a ``FeedMessage``.

    Raises ``ValueError`` with a descriptive message when the response is empty
    or is clearly not protobuf (XML/HTML error pages, JSON), so callers don't see
    an opaque protobuf ``DecodeError``.  Network/HTTP errors propagate as the
    usual ``httpx`` exceptions for the retry layer to classify.
    """
    response = client.get(url)
    response.raise_for_status()

    content = response.content
    content_type = response.headers.get("content-type", "unknown")
    if not content:
        raise ValueError(
            f"Empty response body from {system} feed ({url}): "
            f"status={response.status_code}, content_type={content_type}"
        )

    preview = content[:200]
    if content.startswith(b"<?xml") or content.startswith(b"<Error") or content.startswith(b"<error"):
        raise ValueError(
            f"MTA feed {system} ({url}) returned XML/HTML instead of protobuf: "
            f"status={response.status_code}, content_type={content_type}, "
            f"preview={preview[:80]!r}"
        )
    if content.startswith((b"{", b"[")):
        raise ValueError(
            f"MTA feed {system} ({url}) returned JSON instead of protobuf: "
            f"status={response.status_code}, content_type={content_type}, "
            f"preview={preview[:80]!r}"
        )

    return FeedMessage.FromString(content)
