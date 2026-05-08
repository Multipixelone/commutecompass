"""Tests for llm.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from commutecop.llm import OpencodeGoClient


def _make_response(content: str) -> httpx.Response:
    """Build an httpx Response with a mock HTTP status and request."""
    request = MagicMock(spec=httpx.Request)
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": content}}
            ]
        },
        request=request,
    )


def _make_client() -> OpencodeGoClient:
    return OpencodeGoClient(
        endpoint="https://opencode-go.example/v1/chat/completions",
        token="test-token",
        model="deepseek-v4-flash",
    )


class TestResolveLocation:
    """Tests for resolve_location()."""

    def _call(self, raw: str, content: str, hints: dict | None = None) -> Any:
        """Call client.resolve_location with a mocked HTTP response."""
        hints = hints or {}
        with patch("commutecop.llm.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.post.return_value = _make_response(content)
            client = _make_client()
            return client.resolve_location(raw, hints)

    def test_address_kind_returns_resolved_location(self) -> None:
        result = self._call(
            "200 Example St",
            '{"kind": "address", "value": "200 Example St, New York, NY 10001"}',
        )
        assert result is not None
        assert result.kind == "address"
        assert result.value == "200 Example St, New York, NY 10001"
        assert result.source == "llm"

    def test_station_kind_returns_resolved_location(self) -> None:
        result = self._call(
            "Example Centre",
            '{"kind": "station", "value": "Example LIRR Station, NY"}',
        )
        assert result is not None
        assert result.kind == "station"
        assert result.value == "Example LIRR Station, NY"
        assert result.source == "llm"

    def test_unknown_kind_returns_none(self) -> None:
        result = self._call(
            "somewhere totally obscure",
            '{"kind": "unknown", "value": ""}',
        )
        assert result is None

    def test_fenced_json_parsed_correctly(self) -> None:
        fenced = """```json
        {"kind": "address", "value": "200 W 41st St, New York, NY 10036"}
        ```
        """
        result = self._call("Broadway and 41st", fenced)
        assert result is not None
        assert result.kind == "address"
        assert result.value == "200 W 41st St, New York, NY 10036"

    def test_triple_backtick_fence_parsed_correctly(self) -> None:
        content = """```json
        {"kind": "station", "value": "Jamaica LIRR Station, NY"}
        ```"""
        result = self._call("Jamaica", content)
        assert result is not None
        assert result.kind == "station"

    def test_plain_json_without_fence(self) -> None:
        result = self._call(
            "Columbus Circle",
            '{"kind": "address", "value": "Columbus Circle, New York, NY"}',
        )
        assert result is not None
        assert result.kind == "address"

    def test_malformed_json_returns_none(self) -> None:
        result = self._call("bad json input", "not valid json at all {")
        assert result is None

    def test_missing_kind_field_returns_none(self) -> None:
        result = self._call("no kind field", '{"value": "somewhere"}')
        assert result is None

    def test_empty_value_returns_none(self) -> None:
        result = self._call("empty value", '{"kind": "address", "value": ""}')
        assert result is None

    def test_unexpected_kind_returns_none(self) -> None:
        result = self._call("strange kind", '{"kind": "poi", "value": "Empire State Building"}')
        assert result is None

    def test_timeout_returns_none(self) -> None:
        with patch("commutecop.llm.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.TimeoutException("timed out")
            client = _make_client()
            result = client.resolve_location("any location", {})
            assert result is None

    def test_http_error_returns_none(self) -> None:
        request = MagicMock(spec=httpx.Request)
        err_response = httpx.Response(500, json={"error": "server error"}, request=request)
        with patch("commutecop.llm.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.post.return_value = err_response
            client = _make_client()
            result = client.resolve_location("any location", {})
            assert result is None

    def test_httpx_network_error_returns_none(self) -> None:
        with patch("commutecop.llm.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.ConnectError("connection refused")
            client = _make_client()
            result = client.resolve_location("any location", {})
            assert result is None