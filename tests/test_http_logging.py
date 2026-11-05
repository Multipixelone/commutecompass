"""Offline retained-record tests: MockTransport itself never runs httpcore."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from commutecompass.http_logging import sensitive_http_logging


class RetainingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_httpcore_child_retained_exception_is_safe() -> None:
    """A child handler sees clean objects, not just clean rendered output."""
    logger = logging.getLogger("httpcore.connection.privacy_probe")
    handler = RetainingHandler()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        with sensitive_http_logging():
            try:
                raise RuntimeError("SYNTHETIC_EXCEPTION_PAYLOAD")
            except RuntimeError:
                logger.exception(
                    "connect failed %s", {"url": "SYNTHETIC_HEADER_BODY_URL"},
                    stack_info=True,
                )
        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.args == ()
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
        assert "SYNTHETIC" not in repr(vars(record))
        assert "SYNTHETIC" not in logging.Formatter().format(record)
        assert record.name == logger.name
        assert record.levelno == logging.ERROR
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_context_thread_isolation_and_restoration(caplog: pytest.LogCaptureFixture) -> None:
    barrier = Barrier(2)
    logger = logging.getLogger("httpx")

    def private() -> None:
        with sensitive_http_logging():
            with sensitive_http_logging():
                barrier.wait(timeout=5)
                logger.info("PRIVATE_SENTINEL")
                barrier.wait(timeout=5)
            logger.info("NESTED_PRIVATE_SENTINEL")
        logger.info("AFTER_CONTEXT_VISIBLE")

    def unrelated() -> None:
        barrier.wait(timeout=5)
        logger.info("UNRELATED_CONCURRENT_VISIBLE")
        logging.getLogger("httpx_other").info("PREFIX_LOOKALIKE_VISIBLE")
        barrier.wait(timeout=5)

    with caplog.at_level(logging.DEBUG), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(private), pool.submit(unrelated)]
        for future in futures:
            future.result(timeout=10)
    assert "PRIVATE_SENTINEL" not in caplog.text
    assert "UNRELATED_CONCURRENT_VISIBLE" in caplog.text
    assert "AFTER_CONTEXT_VISIBLE" in caplog.text
    assert "PREFIX_LOOKALIKE_VISIBLE" in caplog.text
    assert "PRIVATE_SENTINEL" not in repr([vars(r) for r in caplog.records])


def test_context_task_isolation_and_exception_cleanup(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        entered = asyncio.Event()
        released = asyncio.Event()

        async def private() -> None:
            with pytest.raises(RuntimeError), sensitive_http_logging():
                entered.set()
                await released.wait()
                logging.getLogger("httpcore.http11").debug("TASK_PRIVATE_SENTINEL")
                raise RuntimeError("synthetic failure")
            logging.getLogger("httpcore.http11").debug("TASK_AFTER_VISIBLE")

        async def unrelated() -> None:
            await entered.wait()
            logging.getLogger("httpcore.http11").debug("TASK_UNRELATED_VISIBLE")
            released.set()

        await asyncio.gather(private(), unrelated())

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert "TASK_PRIVATE_SENTINEL" not in caplog.text
    assert "TASK_AFTER_VISIBLE" in caplog.text
    assert "TASK_UNRELATED_VISIBLE" in caplog.text


def test_factory_chains_once_and_clears_preformatted_content() -> None:
    """Fresh interpreter gives deterministic installation without global test resets."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path[:] = " + repr(sys.path) + "\n" + '''
import logging
from commutecompass.http_logging import sensitive_http_logging
original = logging.getLogRecordFactory()
calls = []
def previous(*args, **kwargs):
    calls.append(1)
    record = original(*args, **kwargs)
    record.message = record.getMessage()
    record.exc_text = "SYNTHETIC_CACHED_SECRET"
    return record
logging.setLogRecordFactory(previous)
with sensitive_http_logging():
    installed = logging.getLogRecordFactory()
    with sensitive_http_logging():
        assert logging.getLogRecordFactory() is installed
        record = logging.getLogger("httpx").makeRecord(
            "httpx", logging.INFO, __file__ if "__file__" in globals() else "probe",
            1, "%s", ("SYNTHETIC_RAW_SECRET",), None)
assert calls == [1]
assert "SYNTHETIC" not in repr(vars(record))
assert "SYNTHETIC" not in logging.Formatter().format(record)
assert logging.getLogRecordFactory() is installed
'''],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
