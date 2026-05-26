"""Cross-process job lock to prevent overlapping morning/poll runs.

Uses ``fcntl.flock`` on a file under the database directory.  Non-blocking:
``acquire()`` raises ``LockHeld`` immediately if another process already holds
the lock, which the CLI translates into an exit-75 (transient) so cron/systemd
simply tries again next cycle.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from types import TracebackType
from typing import Optional, Type


_logger = logging.getLogger(__name__)


class LockHeld(RuntimeError):
    """Raised when the lock is already held by another process."""


class JobLock:
    """File-based job lock; non-blocking acquire."""

    def __init__(self, lock_path: Path | str, *, job_name: str) -> None:
        self.lock_path = Path(lock_path)
        self.job_name = job_name
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockHeld(
                f"{self.job_name} lock {self.lock_path} held by another process"
            )
        # Stamp the holder's pid for debugging.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        _logger.debug("acquired job lock %s for %s pid=%d", self.lock_path, self.job_name, os.getpid())

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()


def lock_path_for(db_path: Path | str, job_name: str) -> Path:
    """Return the lockfile path used by ``job_name`` next to the database."""
    return Path(db_path).parent / f".{job_name}.lock"
