# WT-A — Store & jobs (data integrity)

**Branch / worktree:** `audit/wt-a-store-jobs`
**Write scope (exclusive):** `src/commutecompass/store.py`, `src/commutecompass/joblock.py`,
`src/commutecompass/jobs/poll.py`, `tests/test_store.py`, `tests/test_joblock.py`, `tests/test_jobs.py`, `tests/test_poll*.py`
(create new test files as needed; do not edit files owned by other worktrees).
**Read first:** `AGENTS.md` gotcha 6 ("Ping firing contract") — it is authoritative.
**Validation:** `pytest -q tests/test_store.py tests/test_joblock.py tests/ -k "poll or store or joblock"`, `ruff check .`, `mypy src`.

Do JOB-1 first and commit it on its own (`fix(store): ...`). One commit per item.

## JOB-1 — P0 — init_schema migration deletes ping history on every start
- **Where:** `store.py:199-215` (`init_schema`).
- **Evidence:** the dedupe statement
  `DELETE FROM pings WHERE rowid NOT IN (SELECT MAX(rowid) FROM pings WHERE fired=0 GROUP BY event_id, kind)`
  runs on every `init_schema()` call. The subquery only selects unfired rows, so every
  **fired** ping and every `(event_id, kind)` group without an unfired row is deleted each time
  any job starts. This silently erases fired state; a later replan can re-schedule and re-send
  pings that were already delivered.
- **Fix:**
  1. Only dedupe unfired rows: `DELETE ... WHERE fired = 0 AND rowid NOT IN (<keeper per group among fired=0>)`.
  2. Keeper = latest `fire_at` instant, tiebreak highest rowid
     (`ROW_NUMBER() OVER (PARTITION BY event_id, kind ORDER BY julianday(fire_at) DESC, rowid DESC)`).
  3. Run the dedupe only when the unique index it prepares for does not yet exist
     (check `sqlite_master`), inside the same transaction that creates the index.
- **Tests:** legacy DB fixture (no unique index) with fired rows + duplicate unfired rows →
  after init, fired rows intact and exactly one unfired row per group (the latest). Call
  `init_schema()` twice on a populated modern DB → row count unchanged.

## JOB-4 — P2 — notifier exception strands a claimed actionable ping
- **Where:** `jobs/poll.py` ~294-325, `sent_ok = _notifier.send(ping.message)`.
- **Evidence:** an exception from `send()` is not caught: the ping stays `fired=1` forever
  (never `release_ping`'d, violating the contract) and the rest of the poll aborts.
- **Fix:** wrap `send()`; treat any `Exception` as `sent_ok=False` (log type + ping id, not
  the body), then follow the existing grace-window/attempt-cap release path and continue with
  the next ping.
- **Test:** notifier raising on the first ping, succeeding on the second: first is released
  (`send_attempts` bumped), second delivered, poll returns normally.

## JOB-5 — P2 — sqlite OperationalError aborts poll
- **Where:** `jobs/poll.py:247-325` (`pending_pings` / `claim_ping_entry`).
- **Fix/policy:** a locked/busy DB must not crash the run with a traceback and must **not**
  record a success heartbeat (`record_job_success`). Catch `sqlite3.OperationalError`, log,
  exit with `EXIT_TRANSIENT` (75) semantics via the existing CLI path. Do not retry in a loop.
- **Test:** monkeypatch store method to raise `OperationalError("database is locked")`;
  assert no heartbeat recorded and transient exit.

## JOB-3 — P2 — joblock release can mask the body exception
- **Where:** `joblock.py:49-56`.
- **Fix:** make `release()` idempotent; catch/log `OSError` from `flock`/`os.close` in the
  exit path so the original exception propagates.
- **Test:** body raises `ValueError`, release forced to raise `OSError` → `ValueError` surfaces.

## JOB-7 — P2 — schedule_ping drops send_attempts
- **Where:** `store.py:497-508` INSERT omits `send_attempts` from the `PingEntry`.
- **Fix:** persist it (or document and assert it is always 0 at schedule time). Test round-trip.

## JOB-6 — P2 — concurrency test gaps (tests only)
Add tests to `tests/test_store.py` / poll tests:
- two connections claim the same id → exactly one gets a payload;
- claim returns the *current* payload after the row was rewritten since the snapshot;
- `release_ping` then reclaim succeeds exactly once; attempt cap respected.

## Done when
All items committed separately, validation commands green, no change to the public
`claim_ping` / `claim_ping_entry` signatures.
