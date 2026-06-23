"""Tests for the heartbeat dead-man's-switch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from commutecompass.monitoring import ping_heartbeat


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=MagicMock(spec=httpx.Request))


def test_ping_heartbeat_success() -> None:
    with patch("commutecompass.monitoring.httpx.Client") as mock_cls:
        inst = mock_cls.return_value.__enter__.return_value
        inst.get.return_value = _response(200)
        assert ping_heartbeat("https://hc-ping.example/abc") is True


def test_ping_heartbeat_empty_url_is_noop() -> None:
    with patch("commutecompass.monitoring.httpx.Client") as mock_cls:
        assert ping_heartbeat("") is False
        mock_cls.assert_not_called()


def test_ping_heartbeat_swallows_failure() -> None:
    """A monitoring blip must never raise into the calling job."""
    with patch("commutecompass.monitoring.httpx.Client") as mock_cls, patch(
        "commutecompass.retry.time.sleep"
    ):
        inst = mock_cls.return_value.__enter__.return_value
        inst.get.side_effect = httpx.ConnectError("down")
        assert ping_heartbeat("https://hc-ping.example/abc") is False
