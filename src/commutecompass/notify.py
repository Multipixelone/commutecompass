"""Notifier — emit messages to Telegram or to stdout for OpenClaw to relay."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import httpx

from commutecompass.config import Config


_logger = logging.getLogger(__name__)

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram sendMessage limit is 4096 chars; use a safe target to avoid edge issues
_MAX_MESSAGE_CHARS = 3900

# Stdout-mode delimiters: a downstream wrapper (contrib/openclaw-send.sh) splits
# on these to feed each message to `openclaw message send`.  Picked to be long
# and improbable so message content cannot collide.
STDOUT_MSG_START = "===COMMUTECOMPASS-MSG==="
STDOUT_MSG_END = "===COMMUTECOMPASS-END==="


def _chunk_message(text: str) -> list[str]:
    """Split a message into chunks of at most _MAX_MESSAGE_CHARS chars.

    Prefers splitting on newline boundaries. Falls back to a hard split
    (at _MAX_MESSAGE_CHARS) if no newline is found in the overflow region.
    """
    if len(text) <= _MAX_MESSAGE_CHARS:
        return [text]

    chunks: list[str] = []
    start = 0
    total = len(text)

    while start < total:
        if total - start <= _MAX_MESSAGE_CHARS:
            chunks.append(text[start:])
            break

        # Try to find a newline in the window [start, start + _MAX_MESSAGE_CHARS)
        # splitting on the rightmost newline to keep chunks as full as possible.
        window_end = min(start + _MAX_MESSAGE_CHARS, total)
        split_pos = text.rfind("\n", start, window_end)

        if split_pos > start:
            # Split at newline (include the newline in the chunk)
            chunks.append(text[start:split_pos + 1])
            start = split_pos + 1
        else:
            # Hard split at _MAX_MESSAGE_CHARS boundary
            chunks.append(text[start:window_end])
            start = window_end

    return chunks


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

        Oversized messages (above 3900 chars) are automatically split into
        multiple chunks sent in order. All chunks use the same parse_mode.
        If any chunk fails, returns False.

        Args:
            text: Message text to send.
            parse_mode: Telegram parse mode (default MarkdownV2).

        Returns:
            True if the message was sent successfully (HTTP 200).
            False on any non-200 response or network error.
        """
        url = _TELEGRAM_API_URL.format(token=self.bot_token)
        chunks = _chunk_message(text)

        for chunk in chunks:
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
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


class StdoutNotifier:
    """Emit messages to stdout wrapped in delimiters for OpenClaw to relay.

    Each ``send()`` writes a delimited block to ``stream`` so a tiny wrapper
    (``contrib/openclaw-send.sh``) can split, extract, and pipe each message
    individually to ``openclaw message send``.  Always returns True; if stdout
    is broken the process will die anyway.

    parse_mode is ignored — the upstream Telegram parse mode no longer
    matters here; OpenClaw chooses its own send-side formatting.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        self.stream.write(f"{STDOUT_MSG_START}\n{text}\n{STDOUT_MSG_END}\n")
        self.stream.flush()
        return True


# Loose alias for any object exposing ``send(text, parse_mode) -> bool``.
Notifier = TelegramNotifier | StdoutNotifier


def build_notifier(config: Config) -> Notifier:
    """Construct the notifier dictated by ``config.notify.mode``."""
    if config.notify.mode == "telegram":
        return TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )
    return StdoutNotifier()
