"""Tests for ha_client.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from commutecompass.ha_client import (
    call_service,
    fetch_location,
    fetch_zones,
    push_tomorrow_alarm,
)


def _mock_get_response(status: int, payload: object) -> httpx.Response:
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

    def test_parses_gps_accuracy(self) -> None:
        payload = {
            "entity_id": "person.finn",
            "state": "Work",
            "attributes": {
                "latitude": 40.7346,
                "longitude": -74.0055,
                "gps_accuracy": 12,
            },
            "last_updated": "2026-05-09T12:34:56+00:00",
        }
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = _mock_get_response(200, payload)

            result = fetch_location("http://ha", "person.finn", "tok")

        assert result is not None
        assert result.accuracy_m == 12.0
        assert result.zone == "Work"

    def test_rejects_fuzzier_than_threshold(self) -> None:
        payload = {
            "entity_id": "person.finn",
            "state": "Work",
            "attributes": {
                "latitude": 40.7346,
                "longitude": -74.0055,
                "gps_accuracy": 1500,
            },
            "last_updated": "2026-05-09T12:34:56+00:00",
        }
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = _mock_get_response(200, payload)

            result = fetch_location(
                "http://ha", "person.finn", "tok", min_accuracy_m=500.0
            )

        assert result is None


class TestFetchZones:
    def _states_payload(self) -> list[dict[str, object]]:
        return [
            {
                "entity_id": "zone.home",
                "state": "0",
                "attributes": {
                    "latitude": 40.6798,
                    "longitude": -73.9421,
                    "radius": 100.0,
                    "friendly_name": "Home",
                },
            },
            {
                "entity_id": "zone.work",
                "state": "0",
                "attributes": {
                    "latitude": 40.7346,
                    "longitude": -74.0055,
                    "radius": 128.0,
                    "friendly_name": "Work",
                },
            },
            {
                "entity_id": "device_tracker.nougat",
                "state": "Home",
                "attributes": {"latitude": 40.68, "longitude": -73.94},
            },
            {
                "entity_id": "zone.broken",
                "state": "0",
                "attributes": {"friendly_name": "Broken"},
            },
        ]

    def test_happy_path_filters_to_zones(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = _mock_get_response(200, self._states_payload())

            zones = fetch_zones("http://ha", "tok")

        assert set(zones) == {"home", "work"}
        assert zones["work"].name == "Work"
        assert zones["work"].lat == 40.7346
        assert zones["work"].radius_m == 128.0
        assert zones["work"].entity_id == "zone.work"

    def test_non_200_returns_empty_dict(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.return_value = httpx.Response(status_code=403, text="forbidden")

            assert fetch_zones("http://ha", "tok") == {}

    def test_network_error_returns_empty_dict(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.get.side_effect = httpx.HTTPError("boom")

            assert fetch_zones("http://ha", "tok") == {}

    def test_empty_inputs_short_circuit(self) -> None:
        assert fetch_zones("", "tok") == {}
        assert fetch_zones("http://ha", "") == {}


class TestCallService:
    def test_happy_path_posts_to_service_url(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            ok = call_service(
                "http://ha.local:8123",
                "secret-token",
                "script",
                "commute_alarm",
                data={"title": "Get ready", "message": "Leave in 20"},
            )

        assert ok is True
        call_args = mock_instance.post.call_args
        assert call_args.args[0] == "http://ha.local:8123/api/services/script/commute_alarm"
        assert call_args.kwargs["json"] == {"title": "Get ready", "message": "Leave in 20"}
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"

    def test_trailing_slash_stripped(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            call_service("http://ha/", "tok", "notify", "mobile_app_x")

        assert (
            mock_instance.post.call_args.args[0]
            == "http://ha/api/services/notify/mobile_app_x"
        )

    def test_accepts_201_and_202_as_success(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=201, json=[])

            assert call_service("http://ha", "tok", "script", "s") is True

    def test_4xx_returns_false(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=401, text="unauth")

            assert call_service("http://ha", "tok", "script", "s") is False

    def test_network_error_returns_false(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.HTTPError("boom")

            assert call_service("http://ha", "tok", "script", "s") is False

    def test_empty_inputs_short_circuit(self) -> None:
        assert call_service("", "tok", "script", "s") is False
        assert call_service("http://ha", "", "script", "s") is False
        assert call_service("http://ha", "tok", "", "s") is False
        assert call_service("http://ha", "tok", "script", "") is False

    def test_empty_data_sends_empty_object(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            call_service("http://ha", "tok", "script", "s")

        assert mock_instance.post.call_args.kwargs["json"] == {}


class TestPushTomorrowAlarm:
    def _alarm_at(self) -> datetime:
        return datetime(2026, 5, 26, 5, 42, tzinfo=ZoneInfo("America/New_York"))

    def test_happy_path_posts_iso_datetime(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            ok = push_tomorrow_alarm(
                "http://ha", "tok", "script.commute_set_tomorrow_alarm",
                self._alarm_at(),
            )

        assert ok is True
        call_args = mock_instance.post.call_args
        assert call_args.args[0] == (
            "http://ha/api/services/script/commute_set_tomorrow_alarm"
        )
        assert call_args.kwargs["json"] == {
            "alarm_at": "2026-05-26T05:42:00-04:00",
        }

    def test_extra_data_merged(self) -> None:
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            push_tomorrow_alarm(
                "http://ha", "tok", "script.commute_set_tomorrow_alarm",
                self._alarm_at(),
                extra_data={"label": "commute"},
            )

        body = mock_instance.post.call_args.kwargs["json"]
        assert body["alarm_at"] == "2026-05-26T05:42:00-04:00"
        assert body["label"] == "commute"

    def test_bad_service_returns_false(self) -> None:
        with patch("httpx.Client") as mock_client:
            ok = push_tomorrow_alarm(
                "http://ha", "tok", "no_dot_here", self._alarm_at(),
            )
        assert ok is False
        mock_client.assert_not_called()

    def test_empty_inputs_short_circuit(self) -> None:
        with patch("httpx.Client") as mock_client:
            assert push_tomorrow_alarm("", "tok", "script.x", self._alarm_at()) is False
            assert push_tomorrow_alarm("http://ha", "", "script.x", self._alarm_at()) is False
            assert push_tomorrow_alarm("http://ha", "tok", "", self._alarm_at()) is False
        mock_client.assert_not_called()
