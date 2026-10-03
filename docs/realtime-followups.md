# Real-time commute follow-up work

These are proposed next steps after the realtime safety work and selected-route
timestamp fix at `96f8fae`. They are not implemented capabilities or a settled
architecture. Prioritize the identity bridge below before expanding automatic
delay padding; optional reliability changes need separate scope decisions.

## Completed safety baseline — preserve, do not reopen

- Live observation clocks and elapsed-time arithmetic respect UTC/NYC and DST.
- Provider endpoints and nested boarding timestamps reject invalid placeholders;
  selected-route departure anchors are positive, finite, valid timestamps.
- Fresh, trustworthy measured delay is distinct from unknown, cancellation,
  skipped stops, stale observations, and partial feed failure.
- Poll uses atomic compare-and-swap replanning and claims the current ping
  payload with a live deadline check; expired events cannot revive alarms.
- Same-journey padding increases preserve manual offsets, fired rows, pending
  IDs, and retry state; unknown or reduced delay does not roll alarms back.
- Mypy pre-commit execution is serialized.

Relevant contracts live in [AGENTS.md](../AGENTS.md),
[poll safety notes](../skills/commutecompass/references/poll-replan-safety.md),
[routing](../src/commutecompass/routing.py),
[realtime](../src/commutecompass/realtime.py),
[poll](../src/commutecompass/jobs/poll.py), and
[store](../src/commutecompass/store.py).

## P1 — Bridge ordinary Directions journeys to trusted GTFS identity

**Current gap:** the ordinary Google Directions parser supplies boarding times,
stop names, and line information, but does not independently validate GTFS trip,
route, service date, direction, platform stop, and boarding stop sequence.
The matcher therefore fails closed: ordinary Directions plans are unmatched and
receive zero automatic realtime padding. Automatic matching is not end-to-end
complete merely because the feed reader and poll reconciliation exist.

**Proposed work:** design a bounded journey-identity bridge. Static GTFS schedules
and explicit stop/route/platform/direction mappings are likely inputs, but the
storage, refresh mechanism, dependency choices, and architecture remain open.
Document provenance and ambiguity rules before implementing a mapping. Do not
infer identity from the nearest train, a similar station name, or opposite-direction
service; cancellation must not be treated as an on-time measurement.

Suggested staged scope:

1. Start with a small, explicitly supported subway route/platform set; reconcile
   Directions line names with GTFS routes, platform direction, and service dates.
2. Extend to buses only after route variants, boarding stop sequences, and service
   patterns have explicit matching rules and representative fixtures.
3. Scope LIRR separately: validate branch/service patterns, station/platform
   identity, calendar exceptions, and trip identity rather than assuming subway
   mappings apply. Keep unsupported systems unmatched during rollout.

Acceptance criteria:

- A realistic production-shaped Directions response acquires independently
  trusted GTFS context, matches a same-trip feed observation, persists its plan,
  and lets poll advance the corresponding prep/leave alarms safely.
- Cached and legacy routes without trustworthy context remain fail-closed;
  serialization preserves validated context without manufacturing provenance.
- Multiple plausible trips, same-name stops, and opposite directions are rejected
  when ambiguous. No other journey can supply the selected journey's delay.
- Fresh positive delay and explicit observed zero are valid; missing data,
  cancellation, skipped stops, stale feeds, and partial failures remain distinct
  from observed on-time service and never roll alarms back.
- Hermetic tests cover service-day boundaries and NYC DST transitions, along with
  existing buffer, manual-offset, fired-row, and concurrent-poll contracts.

Start with [routing](../src/commutecompass/routing.py),
[TransitLeg metadata](../src/commutecompass/models.py), and
[matching tests](../tests/test_realtime.py); avoid weakening the matcher to make
an integration demonstration pass.

## P2 — Optional hard response deadline and resource bounds

The five-second fetch budget is an **admission budget**, not a hard wall-clock
bound on streaming response bodies or parsing. HTTPX phase/inactivity timeouts
can allow a slow stream to exceed that budget; parsing can also take extra time.

If operational requirements demand a strict bound, investigate a real end-to-end
deadline plus response-byte and parsing limits. Choose cancellation and cleanup
semantics explicitly rather than promising a bound from per-phase timeouts.

Acceptance: hermetic slow-stream, oversized-body, and malformed-payload tests
demonstrate the chosen limits. Partial usable observations and failure status
survive, later feed admission stays bounded, and alarms never move later because
of a timeout or discarded response. See [fetching](../src/commutecompass/realtime.py)
and [realtime tests](../tests/test_realtime.py).

## P3 — Test hygiene: explicit SQLite connection lifecycles

Validation has emitted unclosed SQLite connection `ResourceWarning`s. This is
evidence to investigate test/fixture cleanup, not proof of a production leak.
Audit explicit `Store` and raw test-connection lifecycles; close resources on
success and failure without hiding warnings or broadening persistence behavior.

Acceptance: full pytest with coverage has no unexplained SQLite resource warnings;
global Ruff, `mypy src tests`, and native Nix validation remain green. Begin with
[fixtures](../tests/conftest.py), [store tests](../tests/test_store.py), and
[job tests](../tests/test_jobs.py).

## P3 — Separate decision: durable notification delivery

Atomic claiming prevents duplicate concurrent poll dispatch, but a process crash
after claim and before delivery can lose a message. Exactly-once delivery is not
guaranteed. This is a separate architecture decision, not a reason to undo the
approved claim/retry contract.

If needed, propose a durable delivery/outbox and idempotency strategy, explicitly
weighing duplicate alerts against missed or stale alarms and notifier support.
Acceptance must include crash-window tests, restart recovery, overlapping polls,
bounded retries, and user-visible delivery tradeoffs. Preserve current behavior
until a replacement contract is agreed. See [poll](../src/commutecompass/jobs/poll.py),
[store](../src/commutecompass/store.py), and [notifiers](../src/commutecompass/notify.py).

## Validation baseline and next-work gate

Native final Nix package/hooks validation passed at `96f8fae`; targeted routing
and planner tests, global Ruff, and mypy also passed for the timestamp fix.
No final full-suite count or coverage percentage is asserted here: the earlier
post-merge coverage run predates that last fix. For each implementation follow-up,
add focused regressions and renew full pytest coverage, Ruff, mypy, and native
Nix validation before claiming completion. Keep dependencies and system coverage
bounded by the approved task rather than expanding all four proposals at once.
