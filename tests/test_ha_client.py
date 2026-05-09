"""Tests for ha_client.py."""

from __future__ import annotations

from typing import Mapping, Optional
from unittest.mock import patch

import httpx

from commutecompass.ha_client import fetch_location


def _mock_get_response(status: int, payload: Optional[Mapping[str, object]]) -> httpx.Response:
    if payload is None:
        return httpx.Response(status_code=status, text="not json")
    return httpx.Response(status_code=status, json=payload)


class TestFetchLocation:
    def test_happy_path_returns_current_location(self) -> None:
        payload = {
            "entity_id": "device_tracker.iphone",
            "state": "not_home",
            "attributes": {"latitude": 40.7128, "longitude": -74.006},
            "last_updated": "2026-05-09T12:34:56.000000+00:00",
        }
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = _mock_get_response(200, payload)

            result = fetch_location(
                "http://ha.local:8123",
                "device_tracker.iphone",
                "secret-token",
            )

        assert result is not None
        assert result.lat == 40.7128
        assert result.lon == -74.006
        assert result.zone == "not_home"
        assert result.source == "home_assistant"

        call_kwargs = mock_instance.get.call_args.kwargs
        assert call_kwargs["headers"]["Authorization"] == "Bearer secret-token"

    def test_missing_coords_returns_none(self) -> None:
        payload = {
            "entity_id": "device_tracker.iphone",
            "state": "home",
            "attributes": {},
            "last_updated": "2026-05-09T12:34:56+00:00",
        }
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = _mock_get_response(200, payload)

            result = fetch_location("http://ha.local:8123", "device_tracker.iphone", "tok")

        assert result is None

    def test_non_200_returns_none(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = httpx.Response(status_code=404, text="Not Found")

            result = fetch_location("http://ha.local:8123", "device_tracker.x", "tok")

        assert result is None

    def test_network_error_returns_none(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.side_effect = httpx.HTTPError("connection refused")

            result = fetch_location("http://ha.local:8123", "device_tracker.x", "tok")

        assert result is None

    def test_empty_inputs_short_circuit(self) -> None:
        assert fetch_location("", "device_tracker.x", "tok") is None
        assert fetch_location("http://ha", "", "tok") is None
        assert fetch_location("http://ha", "device_tracker.x", "") is None
