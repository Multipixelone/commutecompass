# Full-repo audit — 2026-11-05

**Audit status: final audit and detailed implementation handoff.**
Subsequent implementation is tracked separately in [implementation pass 1](implementation-pass-1.md): WT01, WT04, WT07 and WT09 are integrated uncommitted in main, including a snooze compatibility repair. Pass-1 Python checks passed (2,469 tests, 91.38% coverage); its Nix gate initially failed on N-1, now fixed in pass 2. The audit below describes the original audited snapshot, not current remediation completion.
[Implementation pass 2](implementation-pass-2.md) fixes N-1 with a minimal tempfile change and strengthened isolation regression. Focused tests, the full Nix flake check and package rebuild passed on x86_64-linux. Other audit lanes remain outstanding. The implementation records describe the validated pre-commit snapshots; remediation is recorded in separate Conventional Commits in Git history.
Audited source: `bdec0bfa8abe9950bf5473544cae9cab9489412a` (`bdec0bf`). The exact
165-path assignment is in [source-paths.tsv](source-paths.tsv); accepted evidence and
source references are in [findings.md](findings.md), and independent lane briefs are
in [lanes.md](lanes.md).

This pass documents source-verified findings and supplied execution evidence. It is
not an exhaustive security guarantee and does not prove deployment, live-provider, or
runtime behavior. No implementation, worktree, branch, commit or deployment was
made here.

## Results by category

| Category | Scope | Accepted findings | Lane(s) |
|---|---|---:|---|
| A — persistence/jobs | SQLite, plans, locks, morning/poll | A-1–A-4 (4) | [WT01](lanes.md#wt01--persistencejobs) |
| B — calendar/resolution | Calendar fetch, selection, resolver stack | B-1–B-3 (3) | [WT02](lanes.md#wt02--calendar-fetch), [WT03](lanes.md#wt03--selector) |
| C — transit/realtime | MTA, routes, GTFS bridge/index, weather | C-1–C-3 (3) | [WT04](lanes.md#wt04--mta) |
| D — interface | CLI/config/format/notify/skills | D-1, D-3–D-8 (7); D-2 is F-2 alias | [WT05](lanes.md#wt05--configcli), [WT06](lanes.md#wt06--skill-contract), [WT07](lanes.md#wt07--messages) |
| E — alarm/HA | Alarm state, daemon, HA and tomorrow | E-1–E-4 (4) | [WT08](lanes.md#wt08--alarmrecovery) |
| F — platform | Nix, CI, packaging and tracked artifacts | F-1–F-4 (4) | [WT09](lanes.md#wt09--fork-safe-ci), [WT10](lanes.md#wt10--nix-access-contract), [WT11](lanes.md#wt11--coverage-artifact) |
| N | Cross-cutting alarm portability | N-1 (1) | WT08 first substep |

**Canonical total: 26.** D-2 is not an additional finding.

## Evidence and validation state

[baseline.md](baseline.md) records Python PASS results, the Nix flake-check failure,
the cached package result, the N-1 counterfactual probe, and the environment caveat.
The original repository-root `direnv exec .` baseline may have automatically loaded
`.env` through `.envrc`; no credential file was deliberately opened or printed and no
secret value appeared in recorded output. Future validation must use an allowlisted
environment and external dotenv-free flake configuration, not blindly reuse that
command. The Nix failure remains an execution result, not a resolved diagnosis.

The [detailed design contracts](design-decisions.md) resolve WT08 crash recovery and
WT10/WT05 access and configuration semantics. These are selected higher-risk designs,
not additional confirmed defects or validated implementations. The user explicitly
chose to retain them after a complexity review. Background validators own long checks
and the future integrated gate; this audit only validates the documentation handoff.

## Ownership and residual scope

Fixers implement only their lane; testers author focused regressions; validators run
checks; the orchestrator creates/integrates worktrees and obtains approval before
Git mutations. See [lanes](lanes.md). The source review does not cover every possible
provider response, deployment, live service, resolver timeout, malformed GTFS edge,
or the cause of baseline SQLite ResourceWarnings. The stale “not implemented” label in
[realtime-identity-bridge.md](../../realtime-identity-bridge.md) needs a future docs
owner. GTFS path drift and architecture-map omissions in `AGENTS.md` need a separately
authorized agent-policy owner; they are not changed here. Prior fixes are recorded in
[implementation-status.md](../implementation-status.md).

## Inputs and exact coverage

`source-evidence.md` and `source-paths.txt` are supplied appendices. The definitive
category-assigned manifest is [source-paths.tsv](source-paths.tsv). No tracked runtime
`.db` files exist; `.coverage` is tracked and assigned to WT11. Privacy boundary:
synthetic examples only; no personal origins or credentials are reproduced.

## Handoff checklist

- [x] 26 accepted findings, category counts and dispositions documented.
- [x] Exact 165 tracked paths assigned once in the TSV manifest.
- [x] WT01–WT11 have exclusive write sets, dependencies and focused acceptance cases.
- [x] Baseline and privacy caveat recorded; no claim that credentials were not loaded.
- [x] Oracle verdicts for WT08/WT10 incorporated in the detailed design contracts.
- [x] Current audit documentation links, manifest and scope pass final validation.

**Future implementation gate (not performed in this audit):** validators must check
integrated production fixes, including the runtime VM gates. Baseline failures are
not waived; this documentation handoff does not mark any implementation complete.
