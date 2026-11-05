# WT-E — Isolate optional alarm-daemon callbacks

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-alarm-callbacks` / `fix/audit-alarm-callbacks`.
**Exclusive write scope:** `src/commutecompass/alarm_daemon.py`, `tests/test_alarm_daemon.py`.
**Read first:** `docs/alarm-design.md`, `docs/alarm-ha-contract.md`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## ALM-9 — P3 — optional callback exceptions escape run_once

**Confirmed source paths:** handled/window-done publishers at
`alarm_daemon.py:1458,1467`, audio-failed publisher at `:1481`, and heartbeat callback
at `:1484-1488` are invoked without a local guard. State publication through
`_reconcile_mode` (`:1446-1447`, `:1522-1528`) has the same failure mode, reproduced
with an injected raising `publish_state`. A custom/injected callback raising
`RuntimeError` escapes `run_once` and ends `run_forever` after cleanup.

**Severity boundary:** normal Home Assistant HTTP failures already become `False`
in `ha_client.call_service` (`ha_client.py:115-158`). This is defense in depth for
unexpected callback exceptions, not proof that an ordinary HA outage kills the
alarm. Keep the alarm's local ringing independent of these optional publishers.

### Fix boundary

Add a narrow safe-callback boundary that catches `Exception`, logs operation and
exception class without payloads/secrets, and returns failure for retryable
publication paths. Preserve existing pending-publication behavior; a failed callback
must not be reported as acknowledged. Continue propagating `KeyboardInterrupt` and
`SystemExit`. Keep **audio-health gating of heartbeat**: deliberately suppressing
heartbeat after audio failure enables HA's dead-man fallback. Do not alter playback,
wake scheduling, presence gates, or cleanup behavior already fixed.

### Regression tests / acceptance

- Each of state, handled, window-done, audio-failed and heartbeat callbacks raises
  once; `run_once` remains alive, next iteration runs, and later recovery can publish.
- Failed state publication leaves `_published_mode` dirty for retry; isolation is
  per callback, not a catch-all that skips subsequent audio/state work.
- Pending handled/window-done state is not falsely cleared on callback failure.
- A cached due alarm still rings while optional publication fails.
- Audio failure continues suppressing healthy heartbeat; ordinary false-returning
  callbacks retain their existing semantics; process-control exceptions propagate.
- Use fake reader/publishers/audio and frozen aware time; no HA requests.

**Focused command:** `pytest -q tests/test_alarm_daemon.py -k 'heartbeat or handled or window_done or audio_failed or publish_state or mode'`.
**Suggested commit:** `fix(alarm): isolate optional publication callbacks`.

## Legacy audit disposition

ALM-1 was intentional audio-health dead-man gating, now documented; do not reverse it.
ALM-2/3 reader/cleanup guards, ALM-4 safe/redacted HA handling, ALM-5 publication
response checks, ALM-7 atomic state mode preservation, and ALM-8 ring-start cleanup
are resolved. ALM-6 is **partial**: ordinary HA network errors are contained, but
arbitrary non-httpx exceptions in tomorrow's injected publisher were not proven
contained. That residual is an unconfirmed hardening question, not authorization
to expand this lane into `jobs/tomorrow.py`. Geographic validation is independently
owned by [WT-H](wt-h-ha-validation.md).
