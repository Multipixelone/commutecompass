"""Request-local privacy boundary for credential-bearing HTTP operations.

Transport diagnostics can contain URLs, headers, payloads and exception objects.
Replace the entire diagnostic, rather than trying to recognise every secret. A
factory runs before *any* handler (including handlers on child loggers) can retain
those objects. The once-installed factory chains the existing logging factory and
leaves unrelated requests and application loggers untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

_sensitive_http: ContextVar[bool] = ContextVar("sensitive_http", default=False)
_factory_lock = Lock()
_factory_installed = False


def _ensure_factory() -> None:
    global _factory_installed
    with _factory_lock:
        if _factory_installed:
            return
        previous_factory = logging.getLogRecordFactory()

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = previous_factory(*args, **kwargs)
            if _sensitive_http.get() and (
                record.name in ("httpx", "httpcore")
                or record.name.startswith(("httpx.", "httpcore."))
            ):
                record.msg = "sensitive HTTP transport diagnostic omitted"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
                # An earlier chained factory may have cached formatted content.
                record.__dict__.pop("message", None)
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True


@contextmanager
def sensitive_http_logging() -> Iterator[None]:
    """Protect synchronous transport logs in this context, including nested calls.

    ContextVar isolation avoids suppressing unrelated concurrent tasks/threads.
    No logger levels, handlers, or filters are changed. Application diagnostics
    must still use only safe operation/status/exception-class fields.
    """
    _ensure_factory()
    token = _sensitive_http.set(True)
    try:
        yield
    finally:
        _sensitive_http.reset(token)
