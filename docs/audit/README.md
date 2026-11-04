# Repository audit — 2026-11-04

Audit of `commutecompass` at `469bf08`. Six areas were audited in parallel; findings were
spot-checked against source (items with a P0/P1 rating were verified by reading the code).
Baseline at audit time: `ruff check .` clean, `mypy src` clean, `pytest` 1653 passed.

Each brief below is a **self-contained work order for one fixer in its own git worktree**.
Write scopes do not overlap, so worktrees can run in parallel and merge in any order.

| Worktree | Brief | Scope | Top item |
|---|---|---|---|
| `audit/wt-a-store-jobs` | [wt-a-store-jobs.md](wt-a-store-jobs.md) | store, joblock, poll job | **P0 JOB-1**: `init_schema` deletes all fired pings on every start |
| `audit/wt-b-resolution` | [wt-b-resolution.md](wt-b-resolution.md) | llm, geocode, resolver, venues, selector, calendar | P1 LLM parse crash, prompt injection, geocoder null-island |
| `audit/wt-c-routing` | [wt-c-routing.md](wt-c-routing.md) | routing, weather, mta, gtfs_index | P1 malformed transit API responses crash planning |
| `audit/wt-d-interface` | [wt-d-interface.md](wt-d-interface.md) | format, config, openclaw-send.sh, skill | P1 unsanitized feed text in Telegram messages |
| `audit/wt-e-alarm` | [wt-e-alarm.md](wt-e-alarm.md) | alarm, alarm_daemon, ha_client, tomorrow job | P2 daemon dies on reader exception |
| `audit/wt-f-platform` | [wt-f-platform.md](wt-f-platform.md) | nix module, CI, renovate, examples, README | P2 no systemd timeouts; timer timezones |

## Priority order (if not running all in parallel)

1. WT-A (JOB-1 is active data loss — merge first)
2. WT-B, WT-C, WT-D (crash/security hardening of external inputs)
3. WT-E, WT-F

## Rules for every fixer

- Start from `main` in a fresh worktree: `git worktree add ../commutecop-<wt> -b audit/<wt>`.
- Read `AGENTS.md` first; its "Known gotchas" are binding.
- Edit **only** files in your brief's write scope. If a fix seems to need another scope, stop
  and note it in your report instead.
- One Conventional Commit per finding ID (`fix(store): keep fired pings in dedupe migration`),
  ID in the commit body (`Audit: JOB-1`).
- Every behavior change gets a test that fails before the fix.
- Before reporting: `ruff check .`, `mypy src`, and the targeted pytest command in your brief
  all pass; run full `pytest -q` once at the end.
- Items marked **decision / owner check** — follow the stated default and record it in the
  commit body; don't block.
- Report: commits (hash + ID), commands run with exit codes, anything skipped and why.

## Not issues (checked, no action)

- No `%-` strftime specifiers; config allowlist doc in sync today (UI-8 adds a guard test).
- GTFS identity is only set by `gtfs_bridge.validate_leg`; invariant holds.
- HA calls all have finite timeouts and keep the token in headers only.
- No tracked secrets, `.env`, `state.db`, or `result`.
- Bash shebangs in shipped glue are intentional (UI-12, WONTFIX).
