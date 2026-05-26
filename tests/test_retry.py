"""Tests for the retry helper."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from commutecompass.retry import is_transient_http_error, retry


class _Counter:
    def __init__(self, *, raise_n_times: int, exc: Exception, then: Any = "ok") -> None:
        self.calls = 0
        self.raise_n_times = raise_n_times
        self.exc = exc
        self.then = then

    def __call__(self) -> Any:
        self.calls += 1
        if self.calls <= self.raise_n_times:
            raise self.exc
        return self.then


def _make_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://example.test/")
    resp = httpx.Response(status_code=code, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_retry_succeeds_after_one_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single transient 503 is retried once and succeeds on attempt 2."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=1, exc=_make_status_error(503))
    assert retry(fn, attempts=3) == "ok"
    assert fn.calls == 2


def test_retry_succeeds_after_two_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two timeouts then success — total 3 attempts."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=2, exc=httpx.TimeoutException("slow"))
    assert retry(fn, attempts=3) == "ok"
    assert fn.calls == 3


def test_retry_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx is not retried — raises immediately."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=99, exc=_make_status_error(404))
    with pytest.raises(httpx.HTTPStatusError):
        retry(fn, attempts=5)
    assert fn.calls == 1


def test_retry_retries_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 (rate limited) IS treated as transient and retried."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=1, exc=_make_status_error(429))
    assert retry(fn, attempts=3) == "ok"
    assert fn.calls == 2


def test_retry_gives_up_after_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent failure exhausts the budget and re-raises the last error."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=99, exc=_make_status_error(502))
    with pytest.raises(httpx.HTTPStatusError):
        retry(fn, attempts=3)
    assert fn.calls == 3


def test_retry_passes_through_non_http_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """ValueError (programmer error, not transient) is raised immediately."""
    monkeypatch.setattr("time.sleep", lambda _x: None)
    fn = _Counter(raise_n_times=99, exc=ValueError("bad input"))
    with pytest.raises(ValueError):
        retry(fn, attempts=3)
    assert fn.calls == 1


def test_is_transient_http_error_classification() -> None:
    assert is_transient_http_error(httpx.TimeoutException("x")) is True
    assert is_transient_http_error(httpx.NetworkError("x")) is True
    assert is_transient_http_error(_make_status_error(503)) is True
    assert is_transient_http_error(_make_status_error(500)) is True
    assert is_transient_http_error(_make_status_error(429)) is True
    assert is_transient_http_error(_make_status_error(404)) is False
    assert is_transient_http_error(_make_status_error(400)) is False
    assert is_transient_http_error(ValueError("x")) is False
