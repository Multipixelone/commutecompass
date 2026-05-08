"""Tests for notify.py."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from commutecop.notify import TelegramNotifier


class TestTelegramNotifierSend:
    """Tests for TelegramNotifier.send()."""

    def _make_notifier(self) -> TelegramNotifier:
        """Return a notifier with a dummy token and chat ID."""
        return TelegramNotifier(bot_token="123456:ABC-DEF", chat_id=9876543210)

    def test_send_returns_true_on_200(self) -> None:
        """HTTP 200 response returns True."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(status_code=200, json={"ok": True})
            mock_instance.post.return_value = mock_response

            result = notifier.send("Hello, world!")

            assert result is True
            mock_instance.post.assert_called_once()
            call_kwargs = mock_instance.post.call_args.kwargs
            assert call_kwargs["json"]["chat_id"] == 9876543210
            assert call_kwargs["json"]["text"] == "Hello, world!"
            assert call_kwargs["json"]["parse_mode"] == "MarkdownV2"

    def test_send_returns_false_on_500(self) -> None:
        """HTTP 500 response returns False and logs warning."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(
                status_code=500,
                json={"ok": False, "description": "Internal Server Error"},
            )
            mock_instance.post.return_value = mock_response

            result = notifier.send("Hello")

            assert result is False

    def test_send_returns_false_on_400(self) -> None:
        """HTTP 400 response returns False and logs warning."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(
                status_code=400,
                json={"ok": False, "description": "Bad Request"},
            )
            mock_instance.post.return_value = mock_response

            result = notifier.send("Hello")

            assert result is False

    def test_send_returns_false_on_network_error(self) -> None:
        """Network error returns False and logs error."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.HTTPError("Connection refused")

            result = notifier.send("Hello")

            assert result is False

    def test_send_uses_custom_parse_mode(self) -> None:
        """Custom parse_mode is passed to the API."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(status_code=200, json={"ok": True})
            mock_instance.post.return_value = mock_response

            notifier.send("Hello", parse_mode="HTML")

            call_kwargs = mock_instance.post.call_args.kwargs
            assert call_kwargs["json"]["parse_mode"] == "HTML"