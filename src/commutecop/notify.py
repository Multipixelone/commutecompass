"""Telegram notification client."""

from __future__ import annotations


class TelegramNotifier:
    """Client for sending Telegram messages."""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """Send a message. Returns True on success."""
        raise NotImplementedError()