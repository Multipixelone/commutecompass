"""Tests for notify.py."""

from __future__ import annotations

import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest

from commutecompass.config import Config
from commutecompass.format import escape_md, format_alerts_block
from commutecompass.models import Alert
from commutecompass.notify import (
    _MAX_MESSAGE_CHARS,
    STDOUT_MSG_END,
    STDOUT_MSG_START,
    HomeAssistantAlarmNotifier,
    StdoutNotifier,
    TelegramNotifier,
    _chunk_message,
    _strip_markdown_v2,
    build_ha_alarm_notifier,
    build_notifier,
)


@pytest.mark.parametrize("outcome", [200, 400, 503, "network"])
def test_telegram_transport_log_privacy(
    outcome: int | str, caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = TelegramNotifier("SYNTHETIC_BOT_TOKEN", chat_id=246813579)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            raise RuntimeError("SYNTHETIC_TELEGRAM_EXCEPTION")
        except RuntimeError:
            logging.getLogger("httpcore.http11").debug(
                "send %s %s", request.url, request.content, exc_info=True,
            )
        if outcome == "network":
            raise httpx.ConnectError(f"failed {request.url}", request=request)
        assert isinstance(outcome, int)
        return httpx.Response(outcome, json={"ok": True, "description": "SYNTHETIC_BODY"})

    client = httpx.Client(transport=httpx.MockTransport(transport))
    with caplog.at_level(logging.DEBUG), patch("httpx.Client", return_value=client):
        assert notifier.send("SYNTHETIC_MESSAGE") is (outcome == 200)
    assert len(requests) == 1  # no new notifier retries
    assert any(r.name == "httpcore.http11" for r in caplog.records)
    if outcome != "network":
        assert any(r.name == "httpx" for r in caplog.records)
    retained = repr([vars(r) for r in caplog.records])
    for secret in ("SYNTHETIC", "246813579", "api.telegram.org"):
        assert secret not in caplog.text
        assert secret not in retained
    if outcome == "network":
        assert "exception=ConnectError" in caplog.text
    elif outcome != 200:
        assert f"status={outcome}" in caplog.text


def test_private_request_does_not_hide_concurrent_httpx_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both threads use real clients; only the Telegram operation is private."""
    barrier = Barrier(2)
    real_client = httpx.Client

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegram.org":
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
        return httpx.Response(200, json={"ok": True})

    def unrelated() -> None:
        barrier.wait(timeout=5)
        try:
            with real_client(transport=httpx.MockTransport(transport)) as client:
                client.get("https://public.example/UNRELATED_REQUEST_VISIBLE")
        finally:
            barrier.wait(timeout=5)

    with caplog.at_level(logging.DEBUG), patch(
        "httpx.Client",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw),
    ), ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(unrelated)
        private = pool.submit(TelegramNotifier("CONCURRENT_PRIVATE_TOKEN", 246813579).send, "text")
        assert private.result(timeout=10) is True
        other.result(timeout=10)
    assert "UNRELATED_REQUEST_VISIBLE" in caplog.text
    assert "CONCURRENT_PRIVATE_TOKEN" not in caplog.text
    assert "CONCURRENT_PRIVATE_TOKEN" not in repr([vars(r) for r in caplog.records])


@pytest.mark.parametrize(
    "body,accepted",
    [
        (b'{"ok":true}', True),
        (b'{"ok":true,"result":{"message_id":1}}', True),
        (b'{"ok":false,"description":"SYNTHETIC_NEGATIVE_BODY"}', False),
        (b'{"ok":1}', False),
        (b'{"ok":0}', False),
        (b'{"ok":"true"}', False),
        (b'{"ok":null}', False),
        (b'{"ok":[]}', False),
        (b'{"ok":{}}', False),
        (b'{}', False),
        (b'[]', False),
        (b'[{"ok":true}]', False),
        (b'null', False),
        (b'true', False),
        (b'1', False),
        (b'"SYNTHETIC_STRING_BODY"', False),
        (b'<html>SYNTHETIC_MALFORMED_BODY</html>', False),
        (b'', False),
    ],
)
def test_telegram_requires_strict_acknowledgement(
    body: bytes, accepted: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    with caplog.at_level(logging.DEBUG), patch("httpx.Client", return_value=client):
        assert TelegramNotifier("SYNTHETIC_ACK_TOKEN", 246813579).send("SYNTHETIC_TEXT") is accepted
    assert "SYNTHETIC" not in caplog.text
    assert "SYNTHETIC" not in repr([vars(r) for r in caplog.records])
    if not accepted:
        assert "acknowledgement" in caplog.text


@pytest.mark.parametrize("failure_index", [0, 1, 2, None])
@pytest.mark.parametrize("failure_kind", ["negative", "malformed", "http", "network"])
def test_telegram_chunk_acknowledgements_stop_on_first_failure(
    failure_index: int | None, failure_kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    text = "a" * _MAX_MESSAGE_CHARS + "b" * _MAX_MESSAGE_CHARS + "c" * 20
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(request)
        if index == failure_index:
            if failure_kind == "negative":
                return httpx.Response(200, json={"ok": False, "description": "SYNTHETIC_BODY"})
            if failure_kind == "malformed":
                return httpx.Response(200, content=b"SYNTHETIC_INVALID_JSON")
            if failure_kind == "http":
                return httpx.Response(400, content=b"SYNTHETIC_HTTP_BODY")
            raise httpx.ConnectError(f"SYNTHETIC_ERROR {request.url}", request=request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.Client
    with caplog.at_level(logging.DEBUG), patch(
        "httpx.Client",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw),
    ):
        assert TelegramNotifier("SYNTHETIC_CHUNK_TOKEN", 246813579).send(text) is (
            failure_index is None
        )
    assert len(requests) == (3 if failure_index is None else failure_index + 1)
    assert "SYNTHETIC" not in caplog.text
    assert "SYNTHETIC" not in repr([vars(r) for r in caplog.records])


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


# This is a deliberately small *output* grammar, not a chunking algorithm: the
# project emits escaped text, bold and simple links.  Full coverage of Telegram's
# renderer is outside these local tests.  Matching each chunk separately catches
# unmatched entities and dangling escapes without prescribing split positions.
_MD_TEXT = r"(?:\\[\x01-\x7f]|[^\\_*\[\]()~`>#+=|{}.!-])"
_MD_CHUNK_TOKEN = re.compile(
    rf"\\[\x01-\x7f]|\*(?P<bold>{_MD_TEXT}+)\*"
    rf"|\[(?P<label>{_MD_TEXT}+)\]\((?P<url>(?:\\[\\)]|[^\\)\n])+)\)"
    rf"|{_MD_TEXT}"
)


def _render_safe_markdown_chunk(text: str) -> tuple[str, list[str]]:
    """Validate supported MarkdownV2 and recover its text and link targets."""
    visible: list[str] = []
    urls: list[str] = []
    position = 0
    for token in _MD_CHUNK_TOKEN.finditer(text):
        assert token.start() == position, f"Unsafe Markdown near {text[position:position + 40]!r}"
        position = token.end()
        if token.group("url") is not None:
            visible.append(re.sub(r"\\(.)", r"\1", token.group("label")))
            urls.append(re.sub(r"\\(.)", r"\1", token.group("url")))
        elif token.group("bold") is not None:
            visible.append(re.sub(r"\\(.)", r"\1", token.group("bold")))
        else:
            visible.append(re.sub(r"\\(.)", r"\1", token.group()))
    assert position == len(text), f"Unclosed Markdown at {text[position:position + 40]!r}"
    return "".join(visible), urls


def _long_markdown_case(case: str) -> tuple[str, str, list[str]]:
    if case == "generated_alerts":
        headers = [f"Alert {i:02d}: use platform \\B (east). " + "Service changes " * 8
                   for i in range(75)]
        alerts = [Alert(
            id=str(i), header=header, description="", severity="WARNING",
            affected_routes={"C"}, affected_systems=set(), active_periods=[],
        ) for i, header in enumerate(headers)]
        visible = "Active service alerts:\n" + "\n".join(
            f"⚠️ {header}\n  Routes: C" for header in headers
        )
        return format_alerts_block(alerts), visible, []
    if case == "multiline_bold":
        # Each line fits; the complete entity does not. A newline is not
        # automatically a safe place to break a bold entity.
        visible = "\n".join("Platform change " * 100 for _ in range(4))
        return f"*{visible}*", visible, []
    if case == "oversized_link":
        url = "https://maps.example/route?waypoints=" + "40%2C-73%7C" * 500
        return f"[Open directions]({url})", "Open directions", [url]
    if case == "escape_boundary":
        visible = "x" * (_MAX_MESSAGE_CHARS - 1) + ". Next platform \\B. " * 100
        return escape_md(visible), visible, []
    if case == "malformed_bold":
        source = "*Unclosed service update\n" + "Keep original text and punctuation! " * 200
        return source, source, []
    if case == "unsupported_code":
        source = "`" + "Use platform B " * 400 + "`"
        return source, source, []
    if case == "bold_and_links":
        urls = [f"https://maps.example/route/{i}" for i in range(90)]
        titles = [f"Rehearsal {i:02d} (east) \\B" for i in range(90)]
        source = "\n".join(
            f"*{escape_md(title)}*\n[Directions]({url})\n{escape_md('Leave at 7.30!')}\n"
            for title, url in zip(titles, urls, strict=True)
        )
        visible = "\n".join(f"{title}\nDirections\nLeave at 7.30!\n" for title in titles)
        return source, visible, urls
    raise AssertionError(f"Unknown test case: {case}")


@pytest.mark.parametrize("case", [
    "generated_alerts", "multiline_bold", "oversized_link", "escape_boundary", "bold_and_links",
    "malformed_bold", "unsupported_code",
])
def test_long_markdown_delivery_is_independently_safe_and_lossless(case: str) -> None:
    """D5: character/newline splits must not cut bold, URLs or escape pairs.

    Previous length-only chunk tests could pass while Telegram rejected the
    pieces. A safe literal-source fallback may lose styling but not content.
    """
    source, expected_visible, expected_urls = _long_markdown_case(case)
    assert len(source) > _MAX_MESSAGE_CHARS
    payloads: list[dict[str, object]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.Client
    with patch("httpx.Client", side_effect=lambda **kw: real_client(
        transport=httpx.MockTransport(transport), **kw,
    )):
        assert TelegramNotifier("SYNTHETIC_TOKEN", 12345).send(source) is True

    assert len(payloads) > 1
    visible_parts: list[str] = []
    urls: list[str] = []
    for payload in payloads:
        assert payload["chat_id"] == 12345
        chunk = payload["text"]
        assert isinstance(chunk, str)
        assert 0 < len(chunk) <= _MAX_MESSAGE_CHARS
        mode = payload.get("parse_mode")
        if mode == "MarkdownV2":
            visible, targets = _render_safe_markdown_chunk(chunk)
            visible_parts.append(visible)
            urls.extend(targets)
        else:
            assert mode in (None, "")  # an explicitly plain fallback is safe too
            visible_parts.append(chunk)
    rendered = "".join(visible_parts)
    assert (rendered, urls) in [(expected_visible, expected_urls), (source, [])]


@pytest.mark.parametrize("parse_mode", ["HTML", ""])
def test_non_markdown_large_messages_keep_raw_content(parse_mode: str) -> None:
    """D5's Markdown fallback must not start escaping other parse modes."""
    source = "<b>Platform B</b> & \\ corridor!\n" * 300
    payloads: list[dict[str, object]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.Client
    with patch("httpx.Client", side_effect=lambda **kw: real_client(
        transport=httpx.MockTransport(transport), **kw,
    )):
        assert TelegramNotifier("SYNTHETIC_TOKEN", 12345).send(source, parse_mode=parse_mode) is True
    assert len(payloads) > 1
    assert all(payload["parse_mode"] == parse_mode for payload in payloads)
    chunks = [payload["text"] for payload in payloads]
    assert all(isinstance(chunk, str) and 0 < len(chunk) <= _MAX_MESSAGE_CHARS for chunk in chunks)
    assert "".join(str(chunk) for chunk in chunks) == source


def test_safe_markdown_chunks_stop_on_partial_failure_in_order() -> None:
    """D5: safe splitting must not reorder/retry accepted chunks or send after failure."""
    source, _, _ = _long_markdown_case("bold_and_links")
    runs: list[list[dict[str, object]]] = [[], []]
    run = 0

    def transport(request: httpx.Request) -> httpx.Response:
        runs[run].append(json.loads(request.content))
        return httpx.Response(200, json={"ok": not (run == 1 and len(runs[run]) == 2)})

    real_client = httpx.Client
    with patch("httpx.Client", side_effect=lambda **kw: real_client(
        transport=httpx.MockTransport(transport), **kw,
    )):
        notifier = TelegramNotifier("SYNTHETIC_TOKEN", 12345)
        assert notifier.send(source) is True
        assert len(runs[0]) >= 3
        run = 1
        assert notifier.send(source) is False
    assert runs[1] == runs[0][:2]


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

    def test_long_markdown_is_relayed_unchanged_not_telegram_chunked(self) -> None:
        """Telegram safety changes must not escape/chunk the OpenClaw protocol body."""
        source, _, _ = _long_markdown_case("bold_and_links")
        stream = io.StringIO()
        assert StdoutNotifier(stream=stream).send(source) is True
        assert stream.getvalue() == f"{STDOUT_MSG_START}\n{source}\n{STDOUT_MSG_END}\n"

    def test_send_ignores_parse_mode(self) -> None:
        """parse_mode is accepted for protocol compatibility but doesn't affect output."""
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        notifier.send("hi", parse_mode="HTML")
        # No mention of parse_mode in the emitted block
        assert "HTML" not in stream.getvalue()

    def test_send_escapes_delimiter_lines_in_body(self) -> None:
        """A message body containing a literal delimiter line is defused.

        Without this, a calendar event whose title sanitises to the exact
        marker string could trick the contrib relay script into mis-splitting
        the stream or smuggling an extra message into OpenClaw.
        """
        stream = io.StringIO()
        notifier = StdoutNotifier(stream=stream)
        body = f"before\n{STDOUT_MSG_END}\nafter"
        notifier.send(body)
        out = stream.getvalue()
        # The relay splits by exact line match.  Exactly one bare-line MSG and
        # one bare-line END must appear; the embedded occurrence is now
        # prefixed with a zero-width space and therefore does NOT match.
        lines = out.splitlines()
        assert lines.count(STDOUT_MSG_START) == 1
        assert lines.count(STDOUT_MSG_END) == 1
        # "before"/"after" survive, and there is exactly one defused line
        # carrying the original marker.
        assert "before" in lines
        assert "after" in lines
        defused = [ln for ln in lines if ln != STDOUT_MSG_END and STDOUT_MSG_END in ln]
        assert len(defused) == 1
        assert defused[0].startswith("\u200b")

    def test_send_handles_broken_pipe_gracefully(self) -> None:
        """If the downstream pipe is closed, send() returns False instead of raising."""

        class _BrokenStream:
            def write(self, _data: str) -> int:
                raise BrokenPipeError("pipe closed")

            def flush(self) -> None:  # pragma: no cover - never reached
                raise BrokenPipeError("pipe closed")

        notifier = StdoutNotifier(stream=_BrokenStream())  # type: ignore[arg-type]
        assert notifier.send("doomed message") is False


# ── build_notifier dispatch ───────────────────────────────────────────────────


class TestBuildNotifier:
    def _make_config(self, mode: str) -> Config:
        from commutecompass.config import (
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


# ── HomeAssistantAlarmNotifier ────────────────────────────────────────────────


class TestStripMarkdownV2:
    def test_strips_escape_backslashes(self) -> None:
        assert _strip_markdown_v2(r"Leave by 7\.30 \(C train\)") == "Leave by 7.30 (C train)"

    @pytest.mark.parametrize("original", [
        r"Platform \B", "trailing\\", r"two\\slashes", r"\*title*", r"\_name\.",
    ])
    def test_literal_backslashes_survive_plaintext_conversion(self, original: str) -> None:
        """D6: HA must undo Markdown escaping exactly once, not eat original slashes."""
        assert _strip_markdown_v2(escape_md(original)) == original

    def test_leaves_plain_text_unchanged(self) -> None:
        assert _strip_markdown_v2("nothing to do here.") == "nothing to do here."

    def test_unescapes_dash_and_dot(self) -> None:
        assert _strip_markdown_v2(r"prep at 6\.45\-7\.00") == "prep at 6.45-7.00"


class TestHomeAssistantAlarmNotifier:
    def test_send_posts_to_ha_service_with_title_and_message(self) -> None:
        notifier = HomeAssistantAlarmNotifier(
            base_url="http://ha:8123",
            token="tok",
            domain="script",
            service="commute_alarm",
        )
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            ok = notifier.send(
                r"⏰ Get ready for *Rehearsal* \- leave by 7\.30 " + escape_md(r"via \B")
            )

        assert ok is True
        call_args = mock_instance.post.call_args
        assert call_args.args[0] == "http://ha:8123/api/services/script/commute_alarm"
        body = call_args.kwargs["json"]
        assert body["title"] == "CommuteCompass"
        # Backslash escapes stripped; star left as-is (HA doesn't care)
        assert body["message"] == r"⏰ Get ready for *Rehearsal* - leave by 7.30 via \B"

    def test_extra_data_is_merged_and_can_override_title(self) -> None:
        notifier = HomeAssistantAlarmNotifier(
            base_url="http://ha",
            token="tok",
            domain="notify",
            service="mobile_app_iphone",
            extra_data={
                "title": "WAKE UP",
                "data": {"push": {"sound": {"critical": 1, "name": "alarm.caf"}}},
            },
        )
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            notifier.send("leave now")

        body = mock_instance.post.call_args.kwargs["json"]
        assert body["title"] == "WAKE UP"
        assert body["message"] == "leave now"
        assert body["data"]["push"]["sound"]["critical"] == 1

    def test_returns_false_on_ha_failure(self) -> None:
        notifier = HomeAssistantAlarmNotifier(
            base_url="http://ha", token="tok", domain="script", service="s"
        )
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=401, text="nope")

            assert notifier.send("leave now") is False

    def test_parse_mode_is_ignored(self) -> None:
        notifier = HomeAssistantAlarmNotifier(
            base_url="http://ha", token="tok", domain="script", service="s"
        )
        with patch("httpx.Client") as mock_client:
            mock_instance = mock_client.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(status_code=200, json=[])

            notifier.send("hi", parse_mode="HTML")

        # No mention of parse_mode anywhere in the payload
        body = mock_instance.post.call_args.kwargs["json"]
        assert "parse_mode" not in body


class TestBuildHaAlarmNotifier:
    def _make_config(
        self,
        *,
        ha_enabled: bool = True,
        alarm_enabled: bool = True,
        service: str = "script.commute_alarm",
        base_url: str = "http://ha:8123",
        token: str = "tok",
    ) -> Config:
        from commutecompass.config import (
            HomeAssistantAlarmConfig,
            HomeAssistantConfig,
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
            notify=NotifyConfig(mode="stdout"),
            home_assistant=HomeAssistantConfig(
                enabled=ha_enabled,
                base_url=base_url,
                alarm=HomeAssistantAlarmConfig(
                    enabled=alarm_enabled,
                    service=service,
                    extra_data={"data": {"push": {"sound": {"critical": 1}}}},
                ),
            ),
            home_assistant_token=token,
        )

    def test_returns_notifier_when_fully_configured(self) -> None:
        cfg = self._make_config()
        n = build_ha_alarm_notifier(cfg)
        assert n is not None
        assert n.domain == "script"
        assert n.service == "commute_alarm"
        assert n.base_url == "http://ha:8123"
        assert n.token == "tok"
        assert n.extra_data == {"data": {"push": {"sound": {"critical": 1}}}}

    def test_disabled_when_ha_disabled(self) -> None:
        assert build_ha_alarm_notifier(self._make_config(ha_enabled=False)) is None

    def test_disabled_when_alarm_disabled(self) -> None:
        assert build_ha_alarm_notifier(self._make_config(alarm_enabled=False)) is None

    def test_disabled_when_token_missing(self) -> None:
        assert build_ha_alarm_notifier(self._make_config(token="")) is None

    def test_disabled_when_base_url_missing(self) -> None:
        assert build_ha_alarm_notifier(self._make_config(base_url="")) is None

    def test_disabled_when_service_malformed(self) -> None:
        # No dot → not "domain.service"
        assert build_ha_alarm_notifier(self._make_config(service="commute_alarm")) is None
        # Empty
        assert build_ha_alarm_notifier(self._make_config(service="")) is None
        # Dot but missing one side
        assert build_ha_alarm_notifier(self._make_config(service=".commute_alarm")) is None
        assert build_ha_alarm_notifier(self._make_config(service="script.")) is None