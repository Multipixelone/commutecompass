"""Telegram notification client."""

from __future__ import annotations

import logging
from typing import ClassVar

import httpx


_logger = logging.getLogger(__name__)

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Client for sending Telegram messages.

    Args:
        bot_token: Telegram bot token from BotFather.
        chat_id: Target chat ID for messages.
    """

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """Send a message via the Telegram Bot API.

        Args:
            text: Message text to send.
            parse_mode: Telegram parse mode (default MarkdownV2).

        Returns:
            True if the message was sent successfully (HTTP 200).
            False on any non-200 response or network error.
        """
        url = _TELEGRAM_API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            _logger.error("Telegram request failed: %s", exc)
            return False

        if response.status_code != 200:
            _logger.warning(
                "Telegram API returned %d: %s",
                response.status_code,
                response.text[:200],
            )
            return False

        return True