# Baseline execution evidence — 2026-11-05

[Audit index](README.md) · Source `bdec0bfa8abe9950bf5473544cae9cab9489412a`.

**Overall: PARTIAL FAIL.** The orchestrator supplied these results after reading the
validator logs; this documentation session did not execute the commands. The Python
checks passed, but `nix flake check` failed in a test. The hardcoded alarm scratch
root is accepted finding [N-1](findings.md#n-1--dry-run-scratch-path-is-agent-specific).
The focused counterfactual below supports that mechanism; the exact exception
swallowed in the original sandbox run remains inferred, not directly observed.

The Python commands used fresh `direnv exec .` invocations loading the cached Nix
devshell, with Python 3.14.8. The orchestrator reports no tracked changes during the
baseline (these audit docs were written afterward), including no change to tracked
`.coverage`. Local `/tmp/opencode/...` logs are session evidence, **not portable
artifacts**; future fixers should not assume access to them.

| Check | Status / exit | Supplied outcome | Local log |
|---|---|---|---|
| Ruff | PASS / 0 | `All checks passed!` | `/tmp/opencode/agent-run-long.cc-ruff.saKossXznd/output.log` |
| Mypy | PASS / 0 | `Success: no issues found in 85 source files` | `/tmp/opencode/agent-run-long.cc-mypy.mafTYjDT9G/output.log` |
| Pytest with coverage | PASS / 0 | 2380 passed, 22 warnings in 63.66s; 91.08% coverage (log lines 172–217) | `/tmp/opencode/agent-run-long.cc-pytest.C7eV1GcE72/output.log` |
| Nix flake check | FAIL / 1 | Pre-commit Ruff and mypy passed; pytest: 2379 passed, 1 failed (log lines 74–102); about 50s | `/tmp/opencode/agent-run-long.nix-flake-check.slos5KJQIC/output.log` |
| Nix default package | PASS / 0 | Cached result, about 1s; empty log | `/tmp/opencode/agent-run-long.nix-build-default.jHfz3IKtm4/output.log` |

The Python test warnings included sqlite3 `ResourceWarning` messages about unclosed
connections; their cause was not investigated. The failing Nix-check test was
`tests/test_alarm_recovery.py::test_default_dry_run_isolates_provider_writes` at line
255: it expected warning `None`, but received a degraded `AlarmError` fallback.
Read-only diagnosis identified the agent-specific scratch root; the counterfactual
below supports that mechanism without directly revealing the original exception.
The `module-eval` and `renovate-policy` derivations were already valid cached results;
they were not rebuilt. The cached package output was
`/nix/store/dynn6wgk7lfykyxa60pv16gm000ag2rr-commutecompass-0.1.0`; this is not proof
of a fresh build or deployment.

## Environment and privacy

The orchestrator reports that the repository `.envrc` invokes `dotenv_if_exists .env`
and that `.env` exists. The original repository-root `direnv exec .` commands may
therefore have automatically loaded it. No credential files were deliberately opened
or printed, and no secret values were exposed in recorded output; absence of loading
cannot be claimed. A subsequent probe used `env -i`, an external dotenv-free `.envrc`,
and socket blocking. Its counterfactual results and limitations are recorded below.

For future no-secret audit validation, do not blindly run repository-root
`direnv exec .`. The validator should use an explicitly controlled, allowlisted
environment with `nix develop --command`, or an external dotenv-free `.envrc` using
`use flake <repo>`; ensure neither approach loads the repository dotenv and block
network access where required. Preserve the historical commands below as evidence,
not as a privacy-safe procedure to copy.

## N-1 counterfactual probe

The orchestrator reviewed both probe logs. A temporary local harness at
`/tmp/opencode/alarm-counterfactual.lqq0f0jl/probe.py` invoked `pytest.main` only
for `tests/test_alarm_recovery.py::test_default_dry_run_isolates_provider_writes`
and monkeypatched `alarm.TemporaryDirectory`:

| Mode | Supplied result | Local, nonportable log |
|---|---|---|
| `missing-parent` | Recorded `dir='/tmp/opencode'`, injected `FileNotFoundError`; exit 1, one failure with the same warning/assertion | `/tmp/opencode/agent-run-long.alarm-probe-missing-parent.qNTFXsPLL5/output.log`, lines 14–45 |
| `portable-temp` | Removed the explicit `dir` keyword in memory and delegated to `tempfile`; exit 0, one passed in 0.28s | `/tmp/opencode/agent-run-long.alarm-probe-portable-temp.fxpqsQoVYE/output.log`, lines 5–9 |

`TMPDIR` remained `/tmp/opencode`, so this demonstrates influence of the hardcoded
argument through a controlled counterfactual, **not** reproduction of an actually
absent sandbox directory. It does not establish the original swallowed exception,
validate a code patch, or replace full-suite validation. JUnit files were written as
`missing-parent.junit.xml` and `portable-temp.junit.xml` alongside the harness;
all paths are local session artifacts and may not be available later. The orchestrator
reports the source diff check exited 0 after the probe.

The first supplied invocation was (historical evidence, not a portable recipe):

```sh
agent-run-long --label alarm-probe-missing-parent --timeout 10m -- env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" TMPDIR=/tmp/opencode PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 direnv exec /tmp/opencode/alarm-counterfactual.lqq0f0jl python /tmp/opencode/alarm-counterfactual.lqq0f0jl/probe.py missing-parent /tmp/opencode/alarm-counterfactual.lqq0f0jl/missing-parent.junit.xml
```

The second used the same pattern with label `alarm-probe-portable-temp`, mode
`portable-temp`, and `portable-temp.junit.xml`; it exited 0 in about one second
(the first exited 1 in about nine seconds). The external `.envrc` contained only
`use flake /home/tunnel/Documents/Git/commutecop`, the environment was allowlisted,
and socket connections were blocked, according to the supplied evidence.

An independent regression should exercise the dry run when no agent-specific
scratch directory exists, using normal `tempfile` defaults or a controlled `TMPDIR`;
assert private directory permissions (0700), cleanup, and unchanged original DB and
token. Do not hardcode another agent directory. Any setup diagnostic should report
only the exception class, not arbitrary exception text, which may contain credentials.
These are proposed acceptance criteria, not a validated patch.

## Commands recorded

The following are the supplied exact historical baseline commands for comparison,
not instructions to rerun: the repository-root `direnv` invocations may load `.env`.
The temporary coverage path and labels are session-specific. A future validator must
use a controlled environment as above, an appropriate writable temporary coverage
path, and retain its own logs. Expected successful outcomes are exit 0 and passing checks; the Nix flake check
is known to have failed in this baseline and requires investigation before a pass can
be expected.

```sh
agent-run-long --label cc-ruff --timeout 10m -- direnv exec . ruff check .
agent-run-long --label cc-mypy --timeout 15m -- direnv exec . mypy src tests
agent-run-long --label cc-pytest --timeout 20m --excerpt-lines 40 -- direnv exec . env COVERAGE_FILE=/tmp/opencode/cc-cov.0SKthf/.coverage pytest -q --cov=commutecompass --cov-report=term-missing --cov-fail-under=80
agent-run-long --label nix-flake-check --timeout 30m -- nix flake check --print-build-logs
agent-run-long --label nix-build-default --timeout 30m -- nix build .#packages.x86_64-linux.default --no-link --print-build-logs
```

Background validators own long builds, test runs and Nix checks. A full-suite rerun is
not required for this documentation-only handoff; after code fixes, a separate final
integrated gate is required. Neither this baseline nor cached Nix outputs demonstrate
live-service behavior.
