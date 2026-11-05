# Repository audit and isolated fixer handoffs

**Audited source:** `4df247f` (2026-11-04). This replaces the stale audit against
`469bf08`. This pass changes documentation only: no production fixes, deployments,
credential reads, personal-origin inspection, or live calendar/Telegram/HA calls.

Six read-only explorer lanes audited the categories below. Findings were reconciled
against current source; targeted offline probes used fake data and mocked HTTP.
Explorer suggestions that were stale or unsupported were withdrawn rather than
turned into implementation orders. This is a scoped audit, not a security guarantee.

## Categories and outcomes

| Category | Coverage | Current outcome |
|---|---|---|
| A — persistence/jobs | SQLite, locks, morning/poll, heartbeat | Morning outage reconciliation; sensitive heartbeat logs |
| B — calendar/resolution | OAuth, calendars, selectors, resolver, geocode, LLM, venues | Calendar container validation and atomic token writes |
| C — routing/realtime/weather | Directions, GTFS bridge/index, MTA, realtime, weather | Overall endpoint and weather numeric parsing |
| D — interface/config/notification | CLI, TOML/env, Telegram/stdout, OpenClaw skill/glue | Config exit classification; Telegram logging/acknowledgement |
| E — alarm/HA | Wake computation, daemon, publication, presence, tomorrow | Optional callback isolation; geographic input validation |
| F — platform | Nix module/flake, CI, packaging, examples, dependency policy | Check-execution evidence gap; no confirmed production defect |

## Confirmed backlog

P1 = reminder loss or credential exposure; P2 = malformed-input/operational robustness;
P3 = defense in depth. These are audit priorities, not externally assigned CVSS scores.
“Confirmed” means the source path or offline behavior is established, not that a real
user incident or provider failure was observed. Each brief states its evidence level.

| ID | Priority | Finding | Fixer brief |
|---|---|---|---|
| STORE-JOB-1 | P1 | Failed calendar fetch is reconciled as an empty day, cancelling pending reminders | [WT-A](wt-a-store-jobs.md) |
| MONITORING-1 | P1 | Secret-bearing heartbeat URL reaches application/transport logs | [WT-G](wt-g-sensitive-logging.md) |
| NOTIFY-1 | P1 | Telegram bot-token URL reaches exception/transport logs | [WT-G](wt-g-sensitive-logging.md) |
| RES-10 | P2 | Malformed event container escapes per-item handling | [WT-B](wt-b-resolution.md) |
| RES-13 | P2 | OAuth token writes are non-atomic and chmod happens after creation | [WT-B](wt-b-resolution.md) |
| RTE-NEW1 | P2 | Huge overall route endpoint timestamp raises outside parser boundary | [WT-C](wt-c-routing.md) |
| WX-NEW1 | P2 | Huge JSON integer in weather series escapes fail-open fallback | [WT-C](wt-c-routing.md) |
| CFG-1 | P2 | Expected config load errors exit 1 instead of config exit 78 | [WT-D](wt-d-interface.md) |
| CFG-2 | P1 | Existing validation diagnostics expose secret-bearing input URLs | [WT-D](wt-d-interface.md) |
| NOTIFY-3 | P2 | HTTP 200 is accepted without a valid Telegram `ok:true` acknowledgement | [WT-G](wt-g-sensitive-logging.md) |
| ALM-9 | P3 | Unexpected optional publisher/heartbeat callback exceptions escape daemon iteration | [WT-E](wt-e-alarm.md) |
| ALM-10 | P3 | Non-finite/out-of-range HA geometry and invalid accuracy/radius are accepted | [WT-H](wt-h-ha-validation.md) |

**Not confirmed implementation work:** [WT-F](wt-f-platform.md) investigates PLT-8's
zero-check-derivation result and optional fork-PR Attic behavior. Raw LLM input size
and arbitrary non-httpx exceptions in tomorrow's
injected publisher need evidence before expanding scope. Briefs record legacy-ID
resolutions/withdrawals, including intentional dead-man heartbeat suppression.

## Oracle audit corrections

An independent oracle review of `de6475f` confirmed CFG-2 and tightened the briefs:
truthful degraded digests need formatter ownership; route fallbacks must preserve
usable duration-only routes and unavailable provider timing; callback isolation
includes state publication; log tests must exercise real httpx and httpcore child
records without suppressing unrelated logs. These requirements are incorporated
in each owner brief. Malformed individual calendar records still have a separate
completeness tradeoff; Telegram's existing bounded whole-message retries can
repeat earlier chunks. HA geometry work is boundary hardening, not a demonstrated
poll presence failure. No fixes may silently expand into those residual designs.

## Baseline evidence

All commands below completed at audited source, before documentation changes. The
orchestrator inspected the captured outputs. No suite rerun is needed for this
docs-only handoff; future implementation requires the validation gate below.

| Check | Result |
|---|---|
| `pytest -q --cov=commutecompass --cov-report=term-missing --cov-fail-under=80` | **1847 passed**, 9 warnings, 61.28s; 90.54% coverage (terminal total rounds to 91%) |
| `ruff check .` | Passed |
| `mypy src tests` | Passed; 77 source files |
| `nix flake check --print-build-logs` | Exit 0; check attributes evaluated, **0 check derivations executed**; not proof all build checks ran |
| `nix build --no-link .#packages.x86_64-linux.default` | Passed; package/wheel build |

Python checks used `nix develop . --command` (Python 3.14.8). Full logs are local,
not repository artifacts; future agents must rerun rather than assume these paths
exist on another machine:

- `/tmp/opencode/agent-run-long.python-pytest-4df247f.YQHt9eOD2R/output.log`
- `/tmp/opencode/agent-run-long.python-ruff-4df247f.tTiAupJ7w9/output.log`
- `/tmp/opencode/agent-run-long.python-mypy-4df247f.vP9tSvOoUe/output.log`
- `/tmp/opencode/agent-run-long.audit-nix-baseline.tDauheyuYB/output.log`
- `/tmp/opencode/agent-run-long.audit-nix-package-baseline.vtea2AYs0x/output.log`

## Worktree ownership and merge order

The six audit categories become **eight** narrower implementation/investigation
lanes. WT-G owns both secret-bearing HTTP clients so transport-log policy has one
writer; WT-H alone owns shared geographic models. All listed write scopes are
exclusive, including test files. Each brief has focused tests and a validation owner.

| Lane | Worktree slug | Owned application area | Order |
|---|---|---|---|
| A | `audit-morning-preserve` | morning, formatter and their tests | First priority |
| G | `audit-sensitive-http` | monitoring, notify, optional new HTTP-log helper and their tests | First priority; parallel with A |
| B | `audit-calendar` | calendar client and its tests | Can implement in parallel; merge after A |
| C | `audit-provider-parsing` | routing/weather and their tests | Independent |
| D | `audit-config-errors` | config/CLI and their tests | P1 disclosure fix; independent |
| E | `audit-alarm-callbacks` | daemon and its tests | Independent |
| H | `audit-ha-geometry` | models/HA client and their tests | Independent; integration checks consumers |
| F | `audit-check-execution` | no write scope; investigation only | Return evidence; defer full check realization |

A must be integrated before B: invalid calendar containers must produce a controlled
fetch failure, never an authoritative empty list that loses reminders. G owns any
new logging helper; nobody edits `retry.py` on the mistaken premise that it logs
exception text (it already logs class names). If a lane needs another lane's file,
stop and ask the integration owner to transfer scope or schedule a serialized
follow-up. No opportunistic fixes outside a brief.

## Execution rules

1. **Start from the committed handoff.** Integration owner selects one commit containing
   these docs and source based on `4df247f`; use that same start point for all worktrees.
   Read repo `AGENTS.md`, the chosen brief, and relevant existing tests before edits.
   Reproduce each assigned issue with a failing regression test, then implement the
   bounded fix. Findings already fixed since the base get evidence, not duplicate code.
2. **Private data stays private.** Use synthetic config, temporary SQLite databases,
   fake credentials and mocked services. Do not read `.env`, OAuth files, local
   `state.db`/`examples/state.db`, or private origin settings. Do not commit tokens,
   personal addresses/coordinates, captured provider bodies or full logs. This is an
   application repo task, not permission to change host configuration or deploy.
3. **Isolate each writer.** Worktrees belong under `.slim/worktrees/<slug>`, never
   sibling directories. The path is not currently project-ignored; add a **local**
   git exclude first. Run this Python snippet from the primary checkout (idempotent):

   ```python
   from pathlib import Path
   import subprocess
   p = Path(subprocess.check_output(
       ["git", "rev-parse", "--git-path", "info/exclude"], text=True
   ).strip())
   p.parent.mkdir(parents=True, exist_ok=True)
   old = p.read_text() if p.exists() else ""
   rule = "/.slim/worktrees/"
   if rule not in old.splitlines():
       p.write_text(old + ("\n" if old and not old.endswith("\n") else "") + rule + "\n")
   ```

   For example, after selecting the committed handoff as HEAD:

   ```sh
   git worktree add .slim/worktrees/audit-morning-preserve -b fix/audit-morning-preserve HEAD
   git check-ignore .slim/worktrees/audit-morning-preserve
   ```

   Substitute the brief's slug/branch, keep the shared start point fixed, and run
   checks from that worktree. Do not remove or force-clean another agent's worktree.
4. **Validate proportionately.** Lane fixer owns its focused command, lint/type checks
   on touched code, and regression evidence. Load the `agent-run-long` skill on Linux
   before long tests/builds/Nix validation. Delegate each independent long validation
   to a background fixer; serialize commands that share a checkout/cache. Example:

   ```sh
   agent-run-long --label audit-morning-tests --timeout 30m -- nix develop . --command pytest -q tests/test_jobs.py
   ```

   Use outer tool timeout >=1860000ms for 30m; the helper owns timing/logging/status,
   so add no wrappers. Report `label`, `status`, `exit_code`, `duration`, `log_path`,
   and decisive summary/failure excerpt. Report unavailable tooling rather than
   claiming validation passed. Full-suite work belongs to integration, not every lane.
5. **Commit and hand back.** One logical Conventional Commit per finding. Before
   staging, inspect paths and diff for scope/privacy. Return commit IDs, findings
   addressed, changed files, focused evidence, and unresolved risks. Do not merge,
   push or deploy autonomously. Leave worktree removal to the integration owner.

### Ready-to-use fixer prompt

Replace the bracketed values and pass the prompt to an independent fixer:

> Work only in `[absolute .slim/worktrees/slug]` on `[branch]`, from the shared audit
> handoff commit `[commit]`. Read `AGENTS.md`, `docs/audit/README.md`, and
> `docs/audit/[brief].md`. Implement only that brief's confirmed findings within its
> exclusive write scope; WT-F is read-only and returns evidence without a patch.
> You own focused validation and one Conventional Commit per logical fix. Add failing
> offline regression tests before fixing. Preserve privacy and existing public
> contracts. Use the agent-run-long skill/helper for long validation; do not run the
> entire suite or change another lane's files. Return commit IDs, changed paths,
> regression evidence, compact validation reports and any unresolved dependency.
> Integration owner owns merge order, cross-lane review and final validation.

## Integration gate

Reconcile every lane's actual diff before accepting its summary. Merge A before B;
review G's transport logging policy under concurrency and H's model constraints
against all consumers. After all writers settle, run **one final full Python gate**
using the baseline pytest/coverage, Ruff and `mypy src tests` commands. Delegate long
checks to background fixers, serializing overlapping cache/checkout use. Build the
package, and if F changes Nix/CI explicitly realize the intended native check
attributes rather than relying on the zero-check baseline. Compare warnings/coverage
with the baseline; any new failure needs attribution, not a baseline waiver.

Before accepting commits, verify secrets are absent, stdout framing and exit codes
remain compatible, datetimes are aware/NYC-correct, and poll claiming/retry/GTFS
provenance contracts remain intact. Record final evidence and residual investigations.
The tracked `.coverage` file can be dirtied by coverage tooling: inspect and restore
only that generated file when appropriate, never broadly restore a worktree.
