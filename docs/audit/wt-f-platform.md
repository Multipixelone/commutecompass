# WT-F — Optional CI/check-execution investigation

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-check-execution` / `ci/audit-check-execution`.
**Conditional exclusive write scope:** `.github/workflows/ci.yml`,
`.github/actions/setup-nix-attic/action.yml`, `flake.nix`, `nix/module-eval.nix`.
**Validation owner:** this lane's fixer; serialize its Nix builds with integration.
**Status:** investigation first; no confirmed production platform defect warrants a
blind patch. Keep Python/application code out of this lane.

## PLT-8 — unresolved execution-evidence gap, not a missing module test

`flake.nix:70-76` declares `pre-commit-check`, `renovate-policy`, and `module-eval`.
`nix/module-eval.nix:49-75` already asserts path validation, service types, timeouts,
and timer behavior. CI at `.github/workflows/ci.yml:54-58` invokes flake check and
builds the default package.

The audit's local `nix flake check --print-build-logs` exited 0, evaluated these
attributes, but reported **running 0 flake checks**. It therefore does not demonstrate
that their build phases ran. Evaluation itself can force module assertions; do not
claim those assertions were skipped or that hosted CI necessarily behaves like the
local machine. The separate default package build succeeded.

### Investigation and conditional fix

1. Inspect selected system, effective local Nix settings/check selection, and the
   flake output shape. Determine why zero derivations ran locally; distinguish local
   configuration/cache behavior from a reproducible workflow flaw.
2. Explicitly validate the three `checks.x86_64-linux` attributes using the helper
   below. Report evaluated assertions separately from realized derivations/cache hits.
3. If reproducible CI ambiguity remains, make the smallest workflow/check-selection
   change that explicitly validates the intended native checks. Preserve package
   build/Attic behavior and existing module assertions. Otherwise report no change
   needed and retain evidence; do not alter CI for a local-only artifact.

**Focused command (through `agent-run-long`, not a bare long shell call):**

```sh
agent-run-long --label audit-explicit-checks --timeout 30m -- nix build --no-link --print-build-logs '.#checks.x86_64-linux.module-eval' '.#checks.x86_64-linux.renovate-policy' '.#checks.x86_64-linux.pre-commit-check'
```

Use outer tool timeout >=1860000ms; pre-commit-check includes tests and must not run
concurrently with another full validation in the same checkout. The audit did **not**
run this explicit command. Acceptance is evidence for the intended check outputs and
an explained zero-check result, not merely an exit-0 summary. For any workflow edit,
validate syntax and changed expressions locally; do not dispatch hosted CI or deploy.

## F-PLAT-03 — unconfirmed fork-PR Attic resilience question

Setup receives a possibly empty `ATTIC_KEY` at `ci.yml:49-52`. No reproduction proves
that Attic login fails when empty. Investigate with mocked/synthetic credentials
only if useful; guard optional authentication only if a failure is established.
Never print or retrieve repository secrets. This is not a confirmed P1/P2 bug.

## Withdrawals and legacy disposition

- F-PLAT-01 withdrawn: `examples/state.db` is ignored/untracked local state, not a
  tracked DB leak; do not open it. F-PLAT-02 withdrawn: `TimeoutStartSec` for the simple
  daemon is startup-only, not a runtime limit. F-PLAT-04 withdrawn: `OPENCLAW_TARGET`
  exposure is documented nonsecret metadata with an environment-file alternative.
- PLT-1/3: synthetic example identifiers, no confirmed personal-origin/chat-ID leak.
- PLT-2: skill-user group access is an explicit trusted-user boundary, documented in
  `nix/module.nix:303-327` and `README.md:149-156`, not an undisclosed implementation bug.
- PLT-4: `dataDir` restriction and traversal tests exist (`module.nix:331-335`).
- PLT-5: piped service arguments use `escapeShellArg` (`module.nix:87-100`); no new
  exploitable interpolation was demonstrated in fixed Nix-controlled plain commands.
- PLT-6/7: finite oneshot startup timeouts and NYC-qualified timers exist
  (`module.nix:218-237`, `:264-289`). Do not add a daemon runtime limit.
- PLT-9: Renovate automerge is restricted and policy-asserted (`renovate.json:7-29`).
- PLT-10: result links are ignored (`.gitignore:24-25`). PLT-11: README includes alarm
  commands (`README.md:33`). These are resolved/no-finding, not new implementation work.
