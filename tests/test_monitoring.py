"""Tests for the heartbeat dead-man's-switch."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

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


@pytest.mark.parametrize("application_level", [logging.NOTSET, logging.INFO])
@pytest.mark.parametrize("outcome", [200, 403, 503, "network", "retry_success"])
def test_heartbeat_transport_log_privacy(
    outcome: int | str, application_level: int, caplog: pytest.LogCaptureFixture,
) -> None:
    """Real transport logging, under both inherited and CLI-configured levels."""
    caplog.set_level(application_level, logger="commutecompass")
    url = "https://heartbeat.example/SYNTHETIC_HEARTBEAT_TOKEN"
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            raise RuntimeError("SYNTHETIC_HEARTBEAT_BODY")
        except RuntimeError:
            logging.getLogger("httpcore.connection").debug(
                "connect %s", request.url, exc_info=True,
            )
        if outcome == "network" or (outcome == "retry_success" and len(requests) == 1):
            raise httpx.ConnectError(f"failed {url}", request=request)
        status = outcome if isinstance(outcome, int) else 200
        return httpx.Response(status, text="SYNTHETIC_HEARTBEAT_BODY")

    real_client = httpx.Client
    # CLI invocations set the application parent to INFO. Root DEBUG alone
    # cannot enable this child's failure diagnostic; request it explicitly.
    with caplog.at_level(logging.DEBUG), caplog.at_level(
        logging.DEBUG, logger="commutecompass.monitoring",
    ), patch(
        "commutecompass.monitoring.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs),
    ), patch("commutecompass.retry.time.sleep"):
        assert ping_heartbeat(url) is (outcome in (200, "retry_success"))
    assert len(requests) == (2 if outcome in (503, "network", "retry_success") else 1)
    if outcome != "network":
        assert any(r.name == "httpx" for r in caplog.records)
    assert any(r.name == "httpcore.connection" for r in caplog.records)
    assert "SYNTHETIC" not in caplog.text
    assert "SYNTHETIC" not in repr([vars(r) for r in caplog.records])
    if outcome in (503, "network", "retry_success"):
        assert any(r.name == "commutecompass.retry" for r in caplog.records)
    if outcome in (403, 503, "network"):
        assert "exception=" in caplog.text
    if isinstance(outcome, int) or outcome == "retry_success":
        assert all(r.args == () for r in caplog.records if r.name == "httpx")
