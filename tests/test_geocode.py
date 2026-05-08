"""Tests for geocode.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import httpx

from commutecompass.geocode import geocode, GeocodeResult


# ─────────── Mock response builders ───────────

def _ok_response(address: str, lat: float, lon: float, place_id: str = "ChIJxxxx") -> dict[str, Any]:
    return {
        "results": [
            {
                "formatted_address": address,
                "geometry": {
                    "location": {"lat": lat, "lng": lon},
                },
                "place_id": place_id,
            }
        ],
        "status": "OK",
    }


def _zero_results_response() -> dict[str, Any]:
    return {"results": [], "status": "ZERO_RESULTS"}


# ─────────── Tests ───────────

class TestGeocode:
    def test_success_parses_all_fields(self) -> None:
        payload = _ok_response(
            address="200 Example St, New York, NY 10001",
            lat=40.7128,
            lon=-74.0060,
            place_id="ChIJveryLongPlaceIdForTesting",
        )
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.json.return_value = payload
            mock_instance.get.return_value.raise_for_status.return_value = None

            result = geocode("200 Example St, New York, NY", api_key="test-key")

            assert result is not None
            assert isinstance(result, GeocodeResult)
            assert result.formatted_address == "200 Example St, New York, NY 10001"
            assert result.lat == 40.7128
            assert result.lon == -74.0060
            assert result.place_id == "ChIJveryLongPlaceIdForTesting"

            # Verify the request was made correctly
            mock_instance.get.assert_called_once()
            call = mock_instance.get.call_args
            assert call.kwargs["params"]["address"] == "200 Example St, New York, NY"
            assert call.kwargs["params"]["key"] == "test-key"
            assert call.kwargs["params"]["region"] == "us"
            assert call.kwargs["params"]["bounds"] == "40.5,-74.3|41.0,-73.7"

    def test_zero_results_returns_none(self) -> None:
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.json.return_value = _zero_results_response()
            mock_instance.get.return_value.raise_for_status.return_value = None

            result = geocode("nonexistent address xyz 99999", api_key="test-key")

            assert result is None

    def test_zero_results_empty_results_returns_none(self) -> None:
        """API may return OK but with empty results list."""
        payload = {"results": [], "status": "OK"}
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.json.return_value = payload
            mock_instance.get.return_value.raise_for_status.return_value = None

            result = geocode("definitely not a real address", api_key="test-key")
            assert result is None

    def test_transport_error_raises(self) -> None:
        """Connection errors, timeouts, and non-OK HTTP status raise RuntimeError."""
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500 Server Error",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(500),
            )

            with pytest.raises(RuntimeError, match="500"):
                geocode("200 Example St", api_key="test-key")

    def test_timeout_error_raises(self) -> None:
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.side_effect = httpx.TimeoutException("timed out")

            with pytest.raises(RuntimeError):
                geocode("200 Example St", api_key="test-key")

    def test_connection_error_raises(self) -> None:
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.side_effect = httpx.ConnectError("connection refused")

            with pytest.raises(RuntimeError):
                geocode("200 Example St", api_key="test-key")

    def test_non_ok_non_zero_status_raises(self) -> None:
        """e.g. 'INVALID_REQUEST', 'OVER_QUERY_LIMIT', 'REQUEST_DENIED'"""
        payload = {"results": [], "status": "REQUEST_DENIED"}
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.json.return_value = payload
            mock_instance.get.return_value.raise_for_status.return_value = None

            with pytest.raises(RuntimeError, match="REQUEST_DENIED"):
                geocode("200 Example St", api_key="bad-key")

    def test_result_place_id_is_optional(self) -> None:
        """When Google doesn't return a place_id, field is None."""
        payload = {
            "results": [
                {
                    "formatted_address": "123 Main St, Brooklyn, NY 11201",
                    "geometry": {"location": {"lat": 40.6892, "lng": -73.9857}},
                    # no "place_id" key
                }
            ],
            "status": "OK",
        }
        with patch("commutecompass.geocode.httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value.__enter__.return_value
            mock_instance.get.return_value.json.return_value = payload
            mock_instance.get.return_value.raise_for_status.return_value = None

            result = geocode("123 Main St Brooklyn", api_key="test-key")
            assert result is not None
            assert result.place_id is None