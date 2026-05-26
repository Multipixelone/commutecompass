"""Tests for the cross-process job lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from commutecompass.joblock import JobLock, LockHeld, lock_path_for


def test_lock_acquire_release(tmp_path: Path) -> None:
    """A single process can acquire and release the lock."""
    lock = JobLock(tmp_path / ".test.lock", job_name="test")
    lock.acquire()
    lock.release()


def test_lock_reacquire_after_release(tmp_path: Path) -> None:
    """After release, the same path can be acquired again."""
    p = tmp_path / ".test.lock"
    JobLock(p, job_name="test").__enter__().release()  # round-trip
    with JobLock(p, job_name="test"):
        pass  # should not raise


def test_lock_held_raises_on_concurrent_acquire(tmp_path: Path) -> None:
    """When the lock is held by an open fd, a second acquire raises LockHeld."""
    p = tmp_path / ".test.lock"
    holder = JobLock(p, job_name="test")
    holder.acquire()
    try:
        contender = JobLock(p, job_name="test")
        with pytest.raises(LockHeld):
            contender.acquire()
    finally:
        holder.release()


def test_lock_path_for_under_db_dir(tmp_path: Path) -> None:
    """lock_path_for places the lockfile next to the db, named per job."""
    db = tmp_path / "var" / "state.db"
    path = lock_path_for(db, "morning")
    assert path.parent == db.parent
    assert path.name == ".morning.lock"


def test_lock_context_manager_releases_on_exception(tmp_path: Path) -> None:
    """Even if the wrapped block raises, the lock is released."""
    p = tmp_path / ".test.lock"
    with pytest.raises(RuntimeError):
        with JobLock(p, job_name="test"):
            raise RuntimeError("boom")
    # The lock should be reusable now.
    with JobLock(p, job_name="test"):
        pass
