# WT-F — Nix module, CI, repo hygiene, docs

**Branch / worktree:** `audit/wt-f-platform`
**Write scope (exclusive):** `nix/**`, `flake.nix`, `.github/**`, `renovate.json`, `.gitignore`,
`examples/**`, `README.md`.
**Validation:** `nix flake check`, `nix build .#default`, plus any new module-eval check.
Use the `agent-run-long` helper for Nix builds. Baseline before this audit: ruff clean,
mypy clean (35 files), pytest 1653 passed.
**Privacy:** never add a real home address/coordinates or chat IDs anywhere (AGENTS.md rule 1,
`../infra/AGENTS.md` privacy boundary).

## PLT-6 — P2 — no systemd timeouts on oneshot jobs
- **Where:** `nix/module.nix:37-81`. Hung HTTP/OpenClaw keeps the unit active; the poll timer
  cannot start a new run. Add `TimeoutStartSec` per job (e.g. poll 2 min, morning/tomorrow 10 min,
  gtfs-refresh 30 min) — expose as an option with these defaults. App HTTP timeouts must stay below.

## PLT-7 — P2 — timer timezone inconsistent
- **Where:** `nix/module.nix:209-253, 396-426`. Only `gtfsRefreshTime` pins `America/New_York`;
  `morningTime`, `tomorrowTime`, `alarm.publishTime` use host time. Pin defaults to
  `America/New_York` and document that user-supplied `OnCalendar` values should carry a zone.

## PLT-5 — P2 — ExecStart built as an unescaped shell string
- **Where:** `nix/module.nix:87-92`
  (`bash -o pipefail -c '${exe} ${subcommand} | ${sendWrapper}'`). Generate a
  `writeShellApplication` wrapper per job with `lib.escapeShellArg` on every interpolation;
  keep `pipefail`.

## PLT-4 — P2 — dataDir assertion allows traversal
- **Where:** `nix/module.nix:302-305` (`hasPrefix "/var/lib/"`). Also reject `..`, `//`,
  trailing `/`, and require exactly `/var/lib/<name>` (regex `^/var/lib/[A-Za-z0-9._-]+$`, not `.`/`..`).

## PLT-8 — P2 — module not evaluated in CI
- Add a flake `checks.<system>.module-eval` that evaluates a minimal NixOS config importing
  `nixosModules.default` (enable + skill users + alarm) and asserts key service/timer attrs
  (timeouts, OnCalendar zone, dataDir assertion failing for `/var/lib/../etc`). Runs via
  existing `nix flake check` in CI; a VM test is optional/out of scope.

## PLT-2 — P2 (design, document) — skill users can read every service secret
- **Where:** `nix/module.nix:15-35, 280-330`. `skill.users` join `cfg.group`, which must be able
  to read the whole `environmentFile` (0440). This is documented, but the blast radius
  (all API keys, Telegram/HA tokens) is not called out. **Minimum:** state it explicitly in
  the option description and README. **Optional:** add `skill.environmentFile` (separate,
  skill-only secrets) used by the wrapper instead of the service file when set. Do not change
  the default behavior in this pass.

## PLT-9 — P2 — Renovate automerges everything
- **Where:** `renovate.json:7-10`. Keep automerge for patch/minor of dev tooling; disable for
  majors, `github-actions`, `nix` (flake inputs), and `httpx`/`google-auth*`.

## PLT-11 — P3 — README command index misses `alarm`
- **Where:** `README.md:15-32`. Add the `alarm` group (`publish, status, nap, skip, on, off,
  daemon`) and any other missing commands; compare with `commutecompass --help`.

## PLT-1 / PLT-3 — P3 — example identifiers
- `examples/config.toml:11-15`, `examples/config.test.toml:1-5` use "123 Example Ave" with
  Brooklyn coordinates; `examples/nixos-usage.nix:19-22` uses chat `-987654321`. These look
  synthetic. Add a `# synthetic placeholder` comment next to each. **Owner check (not the
  fixer's call):** confirm the coordinates are not near the real origin; if they are, replace
  with a public landmark (e.g. Grand Central) and consider history rewrite.

## PLT-10 — P3 — hygiene
- Add `/result` and `/result-*` to `.gitignore` (currently only ignored by a user-global rule).
