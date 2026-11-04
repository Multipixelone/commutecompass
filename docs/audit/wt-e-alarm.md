# WT-E — Alarm daemon, wake computation, Home Assistant

**Branch / worktree:** `audit/wt-e-alarm`
**Write scope (exclusive):** `src/commutecompass/{alarm,alarm_daemon,ha_client}.py`,
`src/commutecompass/jobs/tomorrow.py`, `docs/alarm-*.md`, and tests
`tests/test_alarm*.py`, `tests/test_ha_client.py`, `tests/test_jobs_tomorrow.py`.
**Read first:** `docs/alarm-design.md`, `docs/alarm-ha-contract.md`.
**Validation:** `pytest -q tests/test_alarm*.py tests/test_ha_client.py tests/test_jobs_tomorrow.py`
(baseline: 483 passed), `ruff check .`, `mypy src`.

## ALM-2 — P2 (do first) — reader exception kills the daemon
- **Where:** `alarm_daemon.py:1342-1346` (`run_once` → `self.reader()`), `run_forever` has only `finally`.
- **Fix:** catch `Exception` from the reader, log type, treat as HA unavailable (`ha_ok=False`)
  so the cached alarm still rings; keep `KeyboardInterrupt`/`SystemExit` propagating.
- **Test:** reader raises `RuntimeError` once then recovers; daemon keeps looping; a cached due
  alarm still enters ringing.

## ALM-3 — P2 — cleanup can skip final mode publication
- **Where:** `alarm_daemon.py:1484-1502` `finally`: `activity_monitor.stop()` unguarded before
  `_stop_audio()` and `_reconcile_mode()`.
- **Fix:** guard each cleanup step independently (try/except-log or `ExitStack`).
- **Test:** monitor `stop()` raises → audio stop and mode publication still happen.

## ALM-1 — P2 — heartbeat stops after audio failure
- **Where:** `alarm_daemon.py:1424-1428` heartbeat gated on `self._audio_healthy`.
- **Decision needed before coding:** check `docs/alarm-design.md` /
  `docs/alarm-ha-contract.md`. If heartbeat loss is the *designed* trigger for HA's dead-man
  fallback on audio failure (the `noqa` comment at ~1391 says "let HA's dead-man fallback fire"),
  this is intentional: document it in the contract doc and add a test that pins it, no code
  change. Otherwise decouple heartbeat from audio health and rely on `audio_failed`. Record which
  in the commit body.

## ALM-4 — P2 — wake horizon is two logical days
- **Where:** `alarm.py:166-195` `compute_wake` raises `AlarmError("no future wake ...")` when both
  today's and tomorrow's wakes are ≤ now.
- **Verify reachability first** (write the failing test): e.g. an early-morning event tomorrow with
  a large lead time computed late at night. If reachable, extend the search to the next logical
  day(s) (bounded, e.g. 3) while keeping the "incomplete planning raises" behavior. If not
  reachable, document the invariant in the docstring and pin it with a test.

## ALM-5 — P2 — "tomorrow" not tested across DST
- **Where:** `jobs/tomorrow.py:57-59` uses `now + timedelta(days=1)`; test mirrors the same
  expression. Compute tomorrow by NYC calendar date (`logical_day_bounds_nyc`), test with frozen
  clocks around 2026-03-08 and 2026-11-01.

## ALM-6 — P2 — HA client error-path tests
- **Where:** `ha_client.py:16-55,110-153,156-196`. Add tests: `httpx.TimeoutException` for each
  call → `None`/`False` without raising; service names must be exactly `domain.service`
  (reject `a.b.c`, `.x`, `x.`); bearer token never appears in log records (use `caplog`).

## ALM-7 — P2 — signal handler tests
- **Where:** `alarm_daemon.py:1596-1607`. Monkeypatch `signal.signal`, capture handlers, invoke:
  SIGTERM stops loop and runs cleanup; SIGUSR1 while idle is a no-op, while ringing dismisses.

## ALM-8 — P3 — legacy tomorrow push vs v1 contract
- **Where:** `jobs/tomorrow.py:14-15,182-196`, `ha_client.py:165-179`, `docs/alarm-ha-contract.md:9-33`.
- **Fix (docs + validation only):** document that the `tomorrow` job uses the legacy
  `{"alarm_at": ...}` payload and is separate from the v1 snapshot publisher; add a config
  validation warning when both are enabled. Do not migrate the payload in this pass.
