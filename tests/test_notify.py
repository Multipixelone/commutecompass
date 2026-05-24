"""Tests for notify.py."""

from __future__ import annotations

import io
from unittest.mock import patch

import httpx

from commutecompass.notify import (
    STDOUT_MSG_END,
    STDOUT_MSG_START,
    StdoutNotifier,
    TelegramNotifier,
    _MAX_MESSAGE_CHARS,
    _chunk_message,
    build_notifier,
)


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


# ── Chunking tests ─────────────────────────────────────────────────────────────


class TestChunkMessage:
    """Tests for _chunk_message helper."""

    def test_short_message_unchanged(self) -> None:
        """Message under limit returns single-element list."""
        text = "Hello, world!"
        result = _chunk_message(text)
        assert result == [text]

    def test_exactly_at_limit_returns_one(self) -> None:
        """Message exactly at limit returns single-element list."""
        text = "x" * _MAX_MESSAGE_CHARS
        result = _chunk_message(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_message_split(self) -> None:
        """Message over limit is split into multiple chunks."""
        text = "x" * (_MAX_MESSAGE_CHARS + 100)
        result = _chunk_message(text)
        assert len(result) == 2
        assert len(result[0]) == _MAX_MESSAGE_CHARS
        assert result[1] == "x" * 100

    def test_split_on_newline(self) -> None:
        """Message is split at newline boundary when available."""
        # 100 "a"s + "\n" + 3900 "b"s = 4001 total → fits in 2 chunks
        text = ("a" * 100) + "\n" + ("b" * 3900)
        result = _chunk_message(text)
        assert len(result) == 2
        # first chunk includes the newline
        assert result[0] == ("a" * 100) + "\n"
        # second chunk is the 3900 "b"s
        assert len(result[1]) == 3900

    def test_multiple_newlines_in_window(self) -> None:
        """When multiple newlines exist in window, splits at the rightmost."""
        # 200 "a"s + "\n" + 200 "b"s + "\n" + 3700 "c"s = 4102 total
        # First window (3900 chars) ends at 3900. Rightmost \n in [0, 3900) is at 401.
        # Second chunk = 4102 - 401 = 3701 chars (< 3900), so exactly 2 chunks.
        text = ("a" * 200) + "\n" + ("b" * 200) + "\n" + ("c" * 3700)
        result = _chunk_message(text)
        assert len(result) == 2
        assert result[0] == ("a" * 200) + "\n" + ("b" * 200) + "\n"
        assert len(result[1]) == 3700

    def test_fallback_hard_split_when_no_newline(self) -> None:
        """Hard split when no newline found near boundary."""
        text = ("a" * (_MAX_MESSAGE_CHARS + 500)).replace("\n", "")
        result = _chunk_message(text)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= _MAX_MESSAGE_CHARS

    def test_three_chunks(self) -> None:
        """Very long message produces three chunks."""
        text = "x" * (_MAX_MESSAGE_CHARS * 2 + 500)
        result = _chunk_message(text)
        assert len(result) == 3


class TestTelegramNotifierSendChunking:
    """Tests for TelegramNotifier.send() chunking behavior."""

    def _make_notifier(self) -> TelegramNotifier:
        return TelegramNotifier(bot_token="123456:ABC-DEF", chat_id=9876543210)

    def test_short_message_single_post(self) -> None:
        """A short message under the limit is sent in one POST."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(status_code=200, json={"ok": True})
            mock_instance.post.return_value = mock_response

            result = notifier.send("Hello, world!")

            assert result is True
            mock_instance.post.assert_called_once()
            assert mock_instance.post.call_args[1]["json"]["text"] == "Hello, world!"

    def test_long_message_splits_into_multiple_posts(self) -> None:
        """An oversized message is sent as multiple POSTs."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_response = httpx.Response(status_code=200, json={"ok": True})
            mock_instance.post.return_value = mock_response

            long_text = "x" * (_MAX_MESSAGE_CHARS + 500)
            result = notifier.send(long_text)

            assert result is True
            assert mock_instance.post.call_count == 2

            # Each chunk should be under the limit
            calls = mock_instance.post.call_args_list
            for call in calls:
                chunk_text = call[1]["json"]["text"]
                assert len(chunk_text) <= _MAX_MESSAGE_CHARS

    def test_chunk_failure_returns_false(self) -> None:
        """If a later chunk fails, send() returns False and no further chunks are sent."""
        notifier = self._make_notifier()
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value

            # First call succeeds, second call fails
            mock_instance.post.side_effect = [
                httpx.Response(status_code=200, json={"ok": True}),
                httpx.Response(status_code=400, json={"ok": False, "description": "Message too long"}),
            ]

            long_text = "x" * (_MAX_MESSAGE_CHARS + 500)
            result = notifier.send(long_text)

            assert result is False
            assert mock_instance.post.call_count == 2


# ── StdoutNotifier ────────────────────────────────────────────────────────────


class TestStdoutNotifier:
    """StdoutNotifier wraps each message in delimiters for OpenClaw to relay."""

    def test_send_writes_delimited_block(self) -> None:
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        ok = notifier.send("hello world")
        assert ok is True
        assert stream.getvalue() == f"{STDOUT_MSG_START}\nhello world\n{STDOUT_MSG_END}\n"

    def test_send_multiline_preserved_inside_block(self) -> None:
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        notifier.send("line 1\nline 2\nline 3")
        out = stream.getvalue()
        assert STDOUT_MSG_START in out
        assert STDOUT_MSG_END in out
        assert "line 1\nline 2\nline 3" in out

    def test_send_multiple_messages_emit_separate_blocks(self) -> None:
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        notifier.send("first")
        notifier.send("second")
        out = stream.getvalue()
        assert out.count(STDOUT_MSG_START) == 2
        assert out.count(STDOUT_MSG_END) == 2

    def test_send_ignores_parse_mode(self) -> None:
        """parse_mode is accepted for protocol compatibility but doesn't affect output."""
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        notifier.send("hi", parse_mode="HTML")
        # No mention of parse_mode in the emitted block
        assert "HTML" not in stream.getvalue()


# ── build_notifier dispatch ───────────────────────────────────────────────────


class TestBuildNotifier:
    def _make_config(self, mode: str):
        from commutecompass.config import (
            Config,
            MtaConfig,
            NotifyConfig,
            OpencodeGoConfig,
            Origin,
            PathsConfig,
            PrepConfig,
            SchedulingConfig,
        )

        return Config(
            origin=Origin(address="x", lat=0.0, lon=0.0),
            calendars=[],
            prep=PrepConfig(),
            scheduling=SchedulingConfig(),
            paths=PathsConfig(venues_file="/tmp/v", db_path="/tmp/d", oauth_token_path="/tmp/t"),
            opencode_go=OpencodeGoConfig(endpoint="https://example.com"),
            mta=MtaConfig(
                subway_alerts_url="https://example.com/s",
                lirr_alerts_url="https://example.com/l",
                bus_alerts_url="https://example.com/b",
            ),
            notify=NotifyConfig(mode=mode),  # type: ignore[arg-type]
            telegram_bot_token="tok",
            telegram_chat_id=12345,
        )

    def test_build_notifier_stdout(self) -> None:
        cfg = self._make_config("stdout")
        assert isinstance(build_notifier(cfg), StdoutNotifier)

    def test_build_notifier_telegram(self) -> None:
        cfg = self._make_config("telegram")
        notifier = build_notifier(cfg)
        assert isinstance(notifier, TelegramNotifier)
        assert notifier.bot_token == "tok"
        assert notifier.chat_id == 12345