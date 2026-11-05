# Audit implementation results

## Review and execution

Two separate oracle sessions reviewed the audit and then planned implementation
lanes. Their corrections were incorporated in `80c30d0`. Each of eight lanes used
its own worktree under `.slim/worktrees/`, based on that exact commit. Seven lanes
implemented fixes; the eighth investigated Nix check execution without changing CI.
The orchestrator inspected changes and focused evidence before cherry-picking the
implementation commits onto `main`.

**Integrated revision:** `f9e728c` (application fixes through `b8f0539`, followed by
two test-only integration corrections). No changes were pushed or deployed.
Worktrees and branches are retained; removing them is a separate cleanup decision.

## Finding disposition

| Lane | Findings | Implemented behavior | Integrated commits |
|---|---|---|---|
| A — morning preservation | STORE-JOB-1 | Fetch failure preserves plans and all ping state, skips reconciliation, and renders an unavailable-calendar notice; successful empty fetch still reconciles | `775e9d2` |
| B — calendar | RES-10, RES-13 | Malformed page/event containers fail the whole fetch; token replacement uses a private same-directory temporary file, flush/fsync, atomic replacement, and cleanup | `1b542f4`, `cc30987` |
| C — provider parsing | RTE-NEW1, WX-NEW1 | Reject invalid/unrepresentable provider timestamps, keep display fallbacks separate from provider timing, and contain overflowing/nonfinite forecast numbers | `0d21533`, `1d7cce7` |
| D — config/CLI | CFG-1, CFG-2 | Expected configuration failures return exit 78 with safe diagnostics; schema validation output excludes raw input and validator context | `8cb6a87`, `b8f0539` |
| E — daemon callbacks | ALM-9 | Optional publication/heartbeat failures remain retryable without interrupting local audio; process-control exceptions still propagate | `6767adc` |
| F — platform investigation | PLT-8 evidence gap | No selection defect established: Nix's zero-build count is cache-aware, not proof of absent checks; no CI patch | Investigation documented in `76c9ef7` |
| G — sensitive HTTP | MONITORING-1, NOTIFY-1, NOTIFY-3 | Request-local transport diagnostics are sanitized before handlers; application logs omit raw URLs/bodies/exceptions; every Telegram chunk requires JSON `ok is True` | `249e007`, `ae1f80a` |
| H — HA geometry | ALM-10 | Current-location/zone models reject nonfinite/out-of-range geometry; ingestion contains invalid values while retaining unknown accuracy and zero-radius semantics | `7115d9a` |

Each implementation lane ran focused offline regression tests, touched-file lint,
and typing before integration. These counts overlap and are not additive:
A 177, B 54, C 286, D 233, E 127, G 94, H 275 tests passed.

## Final integration evidence

Validation targets the integrated code revision above. The subsequent results
update changes only audit documentation, not application code or Nix definitions.

### Python

Initial combined validation at `b8f0539` exposed issues not caught by isolated
checks: three monitoring log assertions failed under full-suite logging state,
and one new morning test inferred a string where a ping-kind literal was required.
The logging failure was a test-capture assumption: CLI initialization leaves the
application parent logger at INFO, suppressing a DEBUG diagnostic even when root
capture is DEBUG. Explicit per-logger capture now tests both inherited and CLI
parent levels without relaxing privacy assertions (`d1c4bcc`). The morning fixture
now explicitly annotates its ping-kind literals (`f9e728c`). That initial run had
2,236 passing tests, three failures, and 90.76% coverage; it was not accepted as a
passing suite.

**Corrected combined validation at `f9e728c` passed:**

| Command (each prefixed with `nix develop . --command`) | Exit | Helper duration | Result |
|---|---|---|---|
| `pytest -q --cov=commutecompass --cov-report=term-missing --cov-fail-under=80` | 0 | 57s | 2,244 passed; coverage 90.76% (80% required) |
| `ruff check .` | 0 | 7s | All checks passed |
| `mypy src tests` | 0 | 3s | No issues in 79 source files |

Pytest reported 19 warnings, including unclosed SQLite connection ResourceWarnings;
these remain a test-resource hygiene follow-up, not a warning-free validation claim.
The tracked `.coverage` artifact was restored after validation rather than committed.

Local helper logs:

- `/tmp/opencode/agent-run-long.final-python-pytest-coverage-f9e728c.kf78X1XbNJ/output.log`
- `/tmp/opencode/agent-run-long.final-python-ruff-f9e728c.I47b720J4m/output.log`
- `/tmp/opencode/agent-run-long.final-python-mypy-f9e728c.ghSOacdgX4/output.log`

### Nix — x86_64-linux

Validated at `b8f0539`; later corrections change only Python tests. Application
source and Nix definitions remain unchanged, so this evidence is retained rather
than repeating the build.

- `nix flake check --no-build --print-build-logs`: exit 0, 8 seconds; evaluation
  passed, including all three declared check attributes.
- `nix build --no-link --print-build-logs
  .#checks.x86_64-linux.module-eval .#checks.x86_64-linux.renovate-policy
  .#packages.x86_64-linux.default`: exit 0, 9 seconds. The module and Renovate check
  outputs were reused; the default package was built locally, including wheel,
  runtime dependency, and import checks.
- `pre-commit-check` was evaluated, but not independently realized in this final
  pass: the separate Python lane runs the full tests/lint/types rather than
  duplicating them through the hook derivation.
- Other system architectures were not checked. Existing check outputs establish
  realization, not fresh execution of their build phases during this pass.

Local helper logs (ephemeral, not uploaded):

- `/tmp/opencode/agent-run-long.commutecop-nix-eval.wpAb51lR96/output.log`
- `/tmp/opencode/agent-run-long.commutecop-nix-build.d7t9yyDt94/output.log`

## Remaining boundaries

- All service regressions use synthetic data/mocked transports; no live integration
  or deployment health claim is made.
- Transport privacy depends on the installed LogRecord factory remaining in the
  logging chain. MockTransport bypasses httpcore, so its child diagnostics were
  probed separately. Protection covers the guarded request context, not arbitrary
  external logging or later replacement factories that discard chaining.
- Existing bounded whole-message retries can duplicate earlier accepted Telegram
  chunks after a later chunk fails; retry semantics were intentionally unchanged.
- Individual malformed calendar records retain the existing skip behavior; the
  new authoritative-fetch failure applies to malformed page/container structure.
- The original audit's unconfirmed suggestions remain unconfirmed, not silently
  promoted into defects or included in the completed-fix count.
