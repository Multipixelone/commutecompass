# Independent fixer briefs — 2026-11-05

Source: `bdec0bfa8abe9950bf5473544cae9cab9489412a`. These are actionable
handoffs, not implementation. Each lane is exclusive: no concurrent writer may touch
a listed path. Fixers implement; testers author worthwhile regressions; background
validators run long checks and the final integrated gate; the orchestrator owns
integration and approval for Git mutations. Findings and source line evidence are in
[findings.md](findings.md).

## Dependency DAG

`WT01 → WT02 → WT08`. After WT01 and WT02 integrate, exclusive `store.py` and
`tests/test_store.py` ownership transfers to WT08. `WT06 → WT10 → WT05` freezes
the broker/base/overlay contract before config implementation; WT10 receives SKILL.md
only after WT06 integrates. WT10's final integration and real permission VM gate
follow integrated WT05. WT09, WT11, WT03, WT04 and WT07 may proceed independently.
`tests/test_jobs.py` is serialized WT01 then WT02. No concurrent writer for shared
paths. The [detailed design contracts](design-decisions.md) are required companion
handoffs; they distinguish chosen higher-risk designs from observed defects.

## WT01 — persistence/jobs

**Findings:** [A-1](findings.md#a-1--failed-send-release-collides-with-replacement) through
[A-4](findings.md#a-4--lexical-datetime-day-membershiporder). **Allowed paths:**
`src/commutecompass/store.py`, `src/commutecompass/jobs/morning.py`,
`src/commutecompass/jobs/poll.py`, `tests/test_store.py`, `tests/test_jobs.py`.

Implement collision-safe release that keeps newer B pending and A consumed; reconcile
only confirmed obsolete reminders while retaining known-good reminders on transient
`no_route`; separate alert processed/delivered state; compare aware instants with
DST-safe ties. Acceptance includes two Store connections, send-failure interleaving,
virtual/past/no-route/empty-fetch cases, alert retry-after-failure and UTC/NY DST
ordering. Run focused `pytest -q tests/test_store.py tests/test_jobs.py` in a
controlled environment. Unverified: concurrency/provider behavior beyond tests.

## WT02 — calendar fetch

**Findings:** [B-1](findings.md#b-1--malformed-timed-event-silently-omitted) and
[B-3](findings.md#b-3--malformed-pagination-metadata-truncates-fetch). **Allowed paths:**
`src/commutecompass/calendar_client.py`, `tests/test_calendar_client.py`,
`tests/test_jobs.py`. Serialize after integrated WT01. Reject malformed authoritative
events and invalid/repeated page tokens without partial success or silent truncation;
preserve cancelled/all-day policy. Test mixed batches, token types, repetition and
morning preservation. Run `pytest -q tests/test_calendar_client.py tests/test_jobs.py`.
Unverified: live Google payloads and provider outages.

## WT03 — selector

**Finding:** [B-2](findings.md#b-2--nonhex-displayed-id-cannot-be-selected). **Allowed
paths:** `src/commutecompass/selector.py`, `tests/test_selector.py`. Support unique
prefixes over the actual ID alphabet, reject ambiguity, exact full ID precedence and
retain title matching. Run `pytest -q tests/test_selector.py`. Do not edit format
files. Unverified: unobserved external ID alphabets.

## WT04 — MTA

**Findings:** [C-1](findings.md#c-1--url-bearing-mta-alert-drops-feed)–[C-3](findings.md#c-3--valid-end-only-active-period-discarded).
**Allowed paths:** `src/commutecompass/mta.py`, `tests/test_mta.py`. Collect repeated
translation URLs; evaluate system/line/time on one leg; preserve end-only active
periods with an aware open bound and reject malformed/inverted values. Request
coordinator scope expansion before `models.py`. Run `pytest -q tests/test_mta.py`.
Unverified: live protobuf schema/provider variation.

## WT05 — config/CLI

**Findings:** [D-1](findings.md#d-1--heartbeat-url-leaks-in-config-show),
[D-3](findings.md#d-3--config-replacement-loses-ownership-and-concurrent-updates),
[D-7](findings.md#d-7--click-usage-exits-2-not-contract-64), and
[D-8](findings.md#d-8--config-edit-parse-errors-are-unnormalized). **Allowed paths:**
`src/commutecompass/config.py`, `src/commutecompass/cli.py`,
`tests/test_config.py`, `tests/test_config_atomic.py`, `tests/test_cli.py`, new
`tests/test_config_overrides.py`. D-3 starts only after WT10 freezes the
[base/overlay contract](design-decisions.md#configuration-semantics-frozen-for-wt05);
D-1/D-7/D-8 can proceed earlier. Implement allowlisted managed overrides, effective
merged validation, base-inheriting unset/reset and unchanged standalone semantics.
Stable sidecar locking covers whole transactions; reset replaces once. Preserve
ownership or fail unchanged. Redact the complete heartbeat URL, normalize usage 64
and safe config-edit 78. Test concurrent edits, invalid base/overlay, absent overlay,
allowlist rejection and mode/owner failures. Run `pytest -q tests/test_config.py tests/test_config_atomic.py tests/test_cli.py tests/test_config_overrides.py`
(the new overlay test path must exist before including it).
Unverified: platform-specific ownership/ACL deployment until VM gate.

## WT06 — skill contract

**Finding:** [D-4](findings.md#d-4--mutating-plan-event-described-as-pure-read).
**Allowed paths:** `skills/commutecompass/SKILL.md`, `tests/test_skill_dispatch.py`.
Document ordinary `plan-event` persistence and retain `--from` preview semantics. Run
`pytest -q tests/test_skill_dispatch.py`. Unverified: model interpretation outside
the contract test.

## WT07 — messages

**Findings:** [D-5](findings.md#d-5--markdown-chunker-splits-entities) and
[D-6](findings.md#d-6--literal-backslash-is-not-escaped). **Allowed paths:**
`src/commutecompass/notify.py`, `src/commutecompass/format.py`,
`tests/test_notify.py`, `tests/test_format.py`. Use bounded entity-safe splitting or
safe fallback; escape literal backslashes without breaking stdout framing or HA plain
text. Run `pytest -q tests/test_notify.py tests/test_format.py`. Unverified: Telegram
renderer behavior beyond local formatting tests.

## WT08 — alarm/recovery

**Findings:** E-1 through E-4 and [N-1](findings.md#n-1--dry-run-scratch-path-is-agent-specific).
**Allowed production paths:** `src/commutecompass/alarm.py`,
`src/commutecompass/alarm_daemon.py`, `src/commutecompass/alarm_state.py`,
`src/commutecompass/jobs/tomorrow.py`, transferred `src/commutecompass/store.py`
only after WT01 and WT02 integrate. **Documentation:** `docs/alarm-design.md`.
**Allowed test paths:** transferred `tests/test_store.py` plus
`tests/test_alarm.py`, `tests/test_alarm_contract.py`, `tests/test_alarm_daemon.py`,
`tests/test_alarm_manual.py`, `tests/test_alarm_manual_controls.py`,
`tests/test_alarm_overlap.py`, `tests/test_alarm_ramp.py`, `tests/test_alarm_recovery.py`,
`tests/test_alarm_state.py`, `tests/test_alarm_status.py`, `tests/test_alarm_window.py`,
`tests/test_jobs_tomorrow.py`. First substep fixes N-1 using normal tempfile/TMPDIR,
0700 cleanup and original DB/token preservation; exception class only in diagnostics.
Then follow the [alarm invariants and acceptance matrix](design-decisions.md#alarm-bounded-write-ahead-association-and-version-pinned-retirement): pre-HA
version-pinned association, atomic success bookkeeping, durable terminal retirement,
idempotent expected-version compare-clear and replacement-safe reconciliation. Do not
claim HA/SQLite atomicity or machine-power-loss durability. E-3 passes explicit failed-day
context, never `None` as `[]`, and preserves no legacy prep push; update the existing
contrary alarm-safety expectation. E-4 logs fixed operation/class, not exception text.
Run `pytest -q tests/test_store.py tests/test_alarm*.py tests/test_jobs_tomorrow.py`
through a validator after ownership transfer. Unverified: live HA and distributed
crash windows beyond injected tests; legacy ambiguity must preserve replacements.

## WT09 — fork-safe CI

**Finding:** [F-1](findings.md#f-1--fork-ci-unconditionally-authenticates-cache).
**Allowed paths:** `.github/actions/setup-nix-attic/action.yml` and, only if required,
`.github/workflows/ci.yml`. Make cache auth conditional on a nonempty credential while
fork checks run without write secrets. Validate by static workflow inspection and the
repository's focused CI configuration checks. Unverified: hosted fork execution.

## WT10 — Nix access contract

**Findings:** [F-2](findings.md#f-2--distinct-skill-user-lacks-stateconfig-access)
and [F-3](findings.md#f-3--documented-optional-secret-target-is-mandatory). **Allowed
paths:** `nix/module.nix`, `nix/module-eval.nix`, `README.md`,
`examples/nixos-usage.nix`, new `nix/skill-broker.sh`, new `tests/test_skill_broker.py`;
transferred `skills/commutecompass/SKILL.md` only after WT06 integrates. Optional new
`nix/skill-access-vm.nix` and `flake.nix` check wiring require coordinator approval
before writing. Implement the [restricted service-UID broker and fixed base/overlay
contract](design-decisions.md#deployment-one-service-uid-restricted-broker-and-persistent-allowlisted-overlay),
not shared writable state. Freeze the config interface for WT05, then complete final
wiring after WT05 integrates. Validate command grammar, argv/env/path injection denial,
credential non-disclosure and service-UID execution; update SKILL trust/reset wording.
Make F-3 target nullable and omit generated env when absent. Run broker tests with
`pytest -q tests/test_skill_broker.py`; validator runs module-eval and the authorized
synthetic VM check after WT05. The VM must exercise distinct users, existing DB/WAL,
mutations, concurrent edits, unauthorized denial, restart and repeated activation.
Module evaluation is insufficient. Migration must quiesce writers and require operator
review of old allowlisted config differences. Unverified: actual host rollout.

## WT11 — coverage artifact

**Finding:** [F-4](findings.md#f-4--generated-coverage-artifact-tracked). **Allowed
paths:** `.gitignore` and removal of tracked `.coverage`. Ignore `.coverage`/`.coverage.*`,
verify tracked lookup fails and ignore lookup succeeds; do not rewrite history or clean
worktrees. Its ignore prerequisite must be coordinated with any managed ignore-block
addition described below. Unverified: historical clones retain the artifact.

## Validation and worktree protocol

Use a controlled allowlisted environment via `nix develop --command` or an external
`use flake /home/tunnel/Documents/Git/commutecop` file; do not blindly use repository
`direnv exec .`, which can load `.env`. Focused commands above are expected outcomes,
not executed results. The orchestrator owns final link/scope checks; validators own
long tests/builds/Nix checks.

Before any worktree or branch creation, rebase/cherry-pick/merge, or removal, the
coordinator must inspect actual status, worktree list and branch names and obtain
explicit user confirmation. Future managed ignore setup is a coordinator-owned,
serialized prerequisite with WT11: add only missing lines `.slim/worktrees/` and
`.slim/worktrees.json` to `.gitignore`, plus the required allowlist in `.ignore`; do
not replace existing rules or perform cleanup.

Illustrative command (**DO NOT RUN WITHOUT EXPLICIT APPROVAL**):

```sh
git worktree add -b audit-20261105-wt01 .slim/worktrees/audit-20261105-wt01 bdec0bfa8abe9950bf5473544cae9cab9489412a
```

A lane starts from that exact SHA or a later integrated prerequisite commit whose
source is verified unchanged. Uncommitted audit docs are not in `bdec0bf`; downstream
fixers need either an integrated prerequisite commit or an accessible read-only
snapshot from main. Do not claim a source-only baseline includes these docs.

## Reusable fixer prompt

> Work only in the coordinator-approved fresh worktree and assigned exclusive paths
> for this lane, from the verified source base and required prerequisites. Implement
> only the assigned findings, preserve privacy, and add focused regression tests.
> Do not edit other lanes, generated outputs, agent policy or audit docs. Tests are
> authored by the tester; long execution belongs to the background validator. Make
> commits **only when explicitly authorized**; otherwise leave an uncommitted patch.
> Each authorized logical commit uses Conventional Commits. Report changed files,
> acceptance evidence, unverified boundaries and dependency requests. Never claim
> execution without supplied logs.
