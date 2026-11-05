# WT-A — Preserve reminders during calendar outages

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-morning-preserve` / `fix/audit-morning-preserve`.
**Exclusive write scope:** `src/commutecompass/jobs/morning.py`, `src/commutecompass/format.py`,
`tests/test_jobs.py`, `tests/test_format.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## STORE-JOB-1 — P1 — failed calendar fetch cancels pending reminders

**Confirmed by source trace:** `morning.py:62-83` converts both `AuthError` and generic
fetch exceptions into `events=[]`. At `:141-150`, reconciliation treats every stored
plan in today's logical NYC day as missing and calls `cancel_pings`, which deletes
all event pings (`store.py:520-526`), including fired history. A transient outage
therefore removes existing prep/leave reminders and history. The auth footer
at `:254-258` does not protect reminder state.

### Fix boundary

Track fetch success separately from its returned list. On failure, do not reconcile
absence, overwrite existing plans, or cancel/reschedule their reminders. Emit a clear
degraded-service digest; retain the specific re-auth instruction for `AuthError`.
The formatter currently asserts “No events scheduled for today” for empty plans
(`format.py:214-216`), even with operations notes. Add a small explicit availability
signal to the formatter (backward-compatible default) or a separate degraded digest;
no brittle string replacement. An unavailable calendar must never be described as a
verified empty day, with or without cached plans. If cached plans are displayed,
they must never re-enter scheduling/upsert loops. A **successful** empty result must
still cancel genuinely removed events. Do not change store transactions or poll contracts.

### Regression tests / acceptance

- Seed a stored plan in today's logical day and pending prep/leave pings. Run morning
  with a transient fetch exception; plan and all ping payloads/fire times/fired state,
  retry counts and fired history survive unchanged.
- Repeat with `AuthError`; preserve reminders and retain the re-auth notice.
- Outages with and without stored plans never emit a verified-empty-day claim.
- Successful `[]` still reconciles deletion and cancels pending reminders.
- Successful nonempty fetch still plans/schedules normally; unrelated days are untouched.
- Use frozen aware NYC time, fake notifier and mocked external clients; no network.

**Focused command:** `pytest -q tests/test_jobs.py tests/test_format.py`.
**Suggested commit:** `fix(morning): preserve reminders when calendar fetch fails`.

## Legacy audit disposition

The old JOB-1/3/4/7 findings are resolved; do not reimplement migration deduplication,
claim-before-send, transactional replans, or reader cleanup. JOB-2's bounded retry
and two-connection claim coverage exist (`tests/test_jobs.py` retry cases and
`tests/test_store.py:test_concurrent_claim_payload_and_release_reclaim`); additional
process-level races are optional coverage, not an open data-loss finding. JOB-5 is resolved at the
CLI boundary (`cli.py:84-86`, `StoreBusyError` → 75). JOB-6 has lock ownership and
serialization implemented; additional process-level lock coverage is optional, not
a confirmed open defect. Relevant safeguards live in `store.py`, `joblock.py`,
`jobs/poll.py`, `tests/test_store.py`, `tests/test_joblock.py`, and poll tests.
