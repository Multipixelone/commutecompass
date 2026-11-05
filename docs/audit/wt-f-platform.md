# WT-F — Optional CI/check-execution investigation

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-check-execution` / `ci/audit-check-execution`.
**Write scope:** none in this round; return investigation evidence only.
**Validation owner:** this lane's fixer for inspection; integration owns check realization.
**Status:** investigation first; no confirmed production platform defect warrants a
blind patch. Keep Python/application code out of this lane.

## PLT-8 — unresolved execution-evidence gap, not a missing module test

`flake.nix:70-76` declares `pre-commit-check`, `renovate-policy`, and `module-eval`.
`nix/module-eval.nix:49-75` already asserts path validation, service types, timeouts,
and timer behavior. CI at `.github/workflows/ci.yml:54-58` invokes flake check and
builds the default package.

The audit's local `nix flake check --print-build-logs` exited 0, evaluated these
attributes, but reported **running 0 flake checks**. It does not demonstrate fresh
build-phase execution. Follow-up inspection of installed Nix 2.35.3's
[`flake.cc`](https://github.com/NixOS/nix/blob/2.35.3/src/nix/flake.cc#L799-L830)
explains that this count includes only `queryMissing().willBuild`: valid or
substitutable checks need not appear. All three exact baseline outputs were valid
locally at follow-up, without establishing whether they were already present or
substitutable at baseline time. No selection defect is confirmed.

Module and Renovate assertions execute during evaluation; the module's synthetic
wrapper commands execute only in its build phase. Do not conflate those evidence
levels or infer hosted CI behavior. The separate default package build succeeded,
but its derivation differs from the evaluation log's package derivation; it does
not prove identical source inputs or pytest execution.

### Investigation only

1. Inspect selected system, effective local Nix settings/check selection, and the
   flake output shape. Determine why zero derivations ran locally; distinguish local
   configuration/cache behavior from a reproducible workflow flaw.
2. Inspect the three `checks.x86_64-linux` attributes without realizing them; the
   integration validation owner coordinates explicit realization because the
   pre-commit check includes the full suite. Distinguish evaluated assertions from
   realized derivations/cache hits in the available evidence.
3. If reproducible CI ambiguity remains, propose (do not implement) the smallest workflow/check-selection
   change that explicitly validates the intended native checks. Preserve package
   build/Attic behavior and existing module assertions. Otherwise report no change
   needed and retain evidence; do not alter CI for a local-only artifact.

**Deferred integration command (not this lane; through `agent-run-long`):**

```sh
agent-run-long --label audit-explicit-checks --timeout 30m -- nix build --no-link --print-build-logs '.#checks.x86_64-linux.module-eval' '.#checks.x86_64-linux.renovate-policy' '.#checks.x86_64-linux.pre-commit-check'
```

Use outer tool timeout >=1860000ms; pre-commit-check includes tests and must not run
concurrently with another full validation in the same checkout. The audit did **not**
run this explicit command. Acceptance is evidence for the intended check outputs and
an explained zero-check result, not merely an exit-0 summary. A workflow patch
requires separately approved scope. Do not dispatch hosted CI or deploy.

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
