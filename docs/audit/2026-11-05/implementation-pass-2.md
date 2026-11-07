# Implementation pass 2 — 2026-11-06

## First slice: N-1 dry-run portability

The user asked to continue after pass 1 was integrated and validated. The immediate goal is to remove the sole remaining Nix flake-check failure before starting further audit lanes. This slice is intentionally smaller than the full WT08 alarm/recovery design.

Base: current main `07217668f23da13f1b66ca6f07dc2e8a368cd9ca` plus pass 1's uncommitted integration. Preserve all existing changes and external commits. No commits, branches, worktrees, deployment or cleanup are authorized by this continuation.

- Fixer owns only `src/commutecompass/alarm.py`: replace the agent-specific dry-run scratch parent with ordinary tempfile/TMPDIR behavior; preserve private scratch cleanup and original database/token isolation.
- Tester owns only `tests/test_alarm_recovery.py`: assess/add a narrow synthetic portability regression without global directory mutation.
- Background validator owns focused alarm tests and lint/type checks, then the previously failing Nix flake gate after the writers are reconciled. Exact commands, statuses and logs will be recorded here.

No raw exception logging, credentials, live APIs or alarm publication/retirement redesign belong in this slice. E-1–E-4 and other outstanding lanes remain queued. N-1's original swallowed exception was inferred; prior counterfactual evidence supported the missing-parent mechanism. A passing real sandbox check is the decisive remaining evidence.

## Reconciled changes

- `alarm.py`: removed only `dir="/tmp/opencode"` from the dry-run `TemporaryDirectory` call. DB/token copying and cleanup behavior are unchanged.
- `tests/test_alarm_recovery.py`: extended the existing isolation regression to select fixture-local TMPDIR, reset tempfile's cached default, wrap the real constructor, and assert default-parent selection, actual scratch placement, mode `0700`, cleanup and original-resource isolation. No duplicate test or global directory mutation.

The orchestrator inspected both diffs; `git diff --check` passed. Writers ran no tests or builds.

## Final validation — PASS

Validated main `07217668f23da13f1b66ca6f07dc2e8a368cd9ca` plus the existing pass-1 integration and these two changed files. HEAD, status, all 166 tracked-file hashes and tracked `.coverage` were unchanged before/after. The Nix source matched every tracked file, including both uncommitted N-1 changes. All-tracked manifest SHA-256: `32e88567532919e2e5ec7f011231672572960484708bfdcd96c82fd4156060f4`.

| Check | Exit | Evidence |
|---|---:|---|
| `pytest -q tests/test_alarm_recovery.py tests/test_alarm.py` | 0 | 114 passed in 2.98s; enhanced isolation regression explicitly passed in JUnit |
| Ruff on the two changed files | 0 | JSON `[]` |
| `nix flake check --print-build-logs` | 0 | Actual pre-commit rebuild: mypy, full pytest and Ruff passed; sandbox enabled |
| `nix build .#packages.x86_64-linux.default --no-link --print-build-logs` | 0 | Actual package rebuild and import check passed |

The successful pre-commit reporter suppresses full pytest counts: the prior 2,469-test/91.38% host result belongs to pass 1 and is not reported as a fresh pass-2 count or coverage measurement. Module-eval and renovate-policy derivations were cached, not rebuilt. Validation covers x86_64-linux only; the other three systems were explicitly omitted.

Commands used `agent-run-long` with sanitized environments and isolated scratch, no repository-root direnv or credential loading. Focused checks used a 10-minute inner/660000ms outer timeout; Nix checks used 30 minutes/1860000ms. Exact environment/command evidence and before/after manifests are preserved under `/tmp/opencode/n1gate.83HYyg/`.

Logs inspected by the orchestrator:
- `/tmp/opencode/agent-run-long.n1-focused-pytest.cbv9hciFx1/output.log`
- `/tmp/opencode/agent-run-long.n1-scoped-ruff.z4lVPyTWMW/output.log`
- `/tmp/opencode/agent-run-long.n1-full-flake-check.35PptpNxPM/output.log`
- `/tmp/opencode/agent-run-long.n1-package-build.dBsQK7MFvd/output.log`

Package output: `/nix/store/3gv3lflrip14d4xzr4r5zzgmxn7x6la2-commutecompass-0.1.0`.

N-1 is fixed and its formerly failing real sandbox gate now passes. E-1–E-4 and the other unimplemented audit lanes remain outstanding; passing checks do not establish live-provider/deployment correctness. All changes remain uncommitted. No deployment, branch/worktree creation, cleanup or credential operation was performed in this slice.
