# AGENTS.md

Guidance for coding agents working in this repository.

## Project summary

`commutecompass` is a Python 3.12+ NYC commute planner that:
- reads Google Calendar events,
- resolves event locations,
- computes transit routes,
- sends Telegram digests/pings,
- reacts to MTA GTFS-RT alerts,
- ships as a Nix flake + NixOS module.

Primary code lives in `src/commutecompass/`, tests in `tests/`, deployment in `nix/`.

## Ground rules

1. **Never commit secrets**
   - Do not commit `.env`, OAuth client JSON files, tokens, or credential dumps.
   - If a secret appears in git changes, stop and remove it from staged files.

2. **Keep changes scoped and reversible**
   - Prefer minimal targeted diffs over broad refactors.
   - Preserve existing public behavior unless the task explicitly changes it.

3. **Add/update tests with behavior changes**
   - If output format, resolver logic, or persistence behavior changes, update tests in the same PR.

4. **Timezone correctness is mandatory**
   - Use timezone-aware datetimes.
   - Keep behavior aligned with `America/New_York` semantics.

## Local workflow

Preferred environment:

```bash
nix develop
```

Common commands:

```bash
# run tests
pytest -q

# run targeted tests
pytest -q tests/test_format.py tests/test_resolver.py

# lint/type-check
ruff check .
mypy src

# CLI
commutecompass --help
commutecompass --config examples/config.toml init-db
commutecompass --config examples/config.toml morning
commutecompass --config examples/config.toml poll
commutecompass --config examples/config.toml tomorrow --dry-run
```

## Architecture map (quick)

- `config.py`: TOML + env loading into `Config`; `redact_for_display`, `update_config_field` for the `config` CLI
- `calendar_client.py`: OAuth/token + Google Calendar fetch
- `resolver.py`: raw location -> cache/venues/geocode/LLM pipeline
- `planner.py`: event -> resolved location -> route -> leave/prep times
- `store.py`: SQLite state (plans, pings, geocode cache, alerts_seen)
- `format.py`: Telegram MarkdownV2-safe messages
- `notify.py`: dispatches between `TelegramNotifier` and `StdoutNotifier` per `[notify].mode`; `build_notifier(config)` is the only entry point job code should use
- `jobs/morning.py`: daily planning + digest
- `jobs/poll.py`: due pings + alert-triggered replanning
- `jobs/tomorrow.py`: evening push of tomorrow's earliest prep_at to an HA script (pull-model wake alarm)
- `skills/commutecompass/`: OpenClaw skill (SKILL.md + scripts/ + references/); model-invoked dispatch for chat queries and adjustments
- `contrib/openclaw-send.sh`: cron/systemd glue that splits stdout-mode messages and pipes each one to `openclaw message send`

## Known gotchas

1. **OAuth env format**
   - `GOOGLE_OAUTH_CLIENT_SECRET` is expected to be a **JSON string** (client config), not just the raw secret token.

2. **Telegram MarkdownV2 is strict**
   - Escape user/content fields with `escape_md()`.
   - Literal markdown-sensitive punctuation in formatted templates (e.g. `(` and `)`) must also be escaped when needed.
   - Always wrap user-supplied strings (event titles, calendar names, alert
     headers, locations) with `_sanitize_text(...)` *before* `escape_md`.
     The sanitiser strips control chars, Unicode bidi overrides, and
     truncates to 200 chars.

3. **Non-actionable locations**
   - Placeholder locations like “Location available once RSVP’d” should short-circuit in resolver (no unnecessary LLM call).

4. **Portable strftime**
   - Do not introduce `%-I` or `%-d` strftime specifiers — they are GNU
     extensions that raise on macOS and Windows.  Use the `_fmt_time` /
     `_fmt_day_of_month` helpers in `format.py` or `.strftime("%I:%M %p").lstrip("0")`.
     `tests/test_format.py` has a regression test that fails on `%-`.

5. **CLI exit codes**
   - `cli.py` exposes `EXIT_OK=0`, `EXIT_USAGE=64`, `EXIT_NOT_FOUND=65`,
     `EXIT_UNRESOLVED=66`, `EXIT_TRANSIENT=75`, `EXIT_CONFIG=78`.  New
     `sys.exit()` calls should use these so agent callers can distinguish
     failure modes without parsing logs.

6. **Ping firing contract**
   - Use `store.claim_ping(id, now)` (atomic 0→1 transition) before
     sending a notification, not `mark_fired` after.  This is the race
     protection against overlapping poll cycles.
   - On send **failure** of an actionable ping (`prep`/`leave`), the poll
     loop hands the row back with `store.release_ping(id)` (atomic 1→0,
     bumps `send_attempts`) so a later poll re-fires it.  Re-fire is bounded:
     only within `_SEND_RETRY_GRACE_SECONDS` of the scheduled `fire_at` (a
     stale alarm is worse than none) and only up to `_MAX_SEND_ATTEMPTS`
     (a broken notifier can never storm).  Other kinds, and pings past the
     grace window or attempt cap, stay fired (give up) — never retried.

7. **OpenClaw stdout protocol**
   - `notify.StdoutNotifier` escapes any body line that exactly matches
     `STDOUT_MSG_START` / `STDOUT_MSG_END` with a zero-width space.  If
     you add a new framing marker, also extend `_escape_stdout_delimiters`.

## Code style expectations

- Python: type hints required; keep mypy strict-compatible.
- Keep functions small and explicit.
- Log failures with useful context, but avoid noisy logs for known non-actionable cases.
- Do not add new dependencies unless necessary.

## When changing behavior

If you change any of these, update tests and mention in PR notes:
- message formatting (`format.py`),
- location resolution order/heuristics (`resolver.py`),
- DB schema or serialization (`store.py`),
- scheduling semantics in jobs,
- new CLI subcommands or removed ones — update `skills/commutecompass/SKILL.md` (dispatch table) and add/remove the corresponding script under `skills/commutecompass/scripts/`,
- `CONFIG_SET_ALLOWLIST` in `config.py` — keep `skills/commutecompass/references/config-allowlist.md` in sync,
- the stdout-mode delimiters in `notify.py` — `contrib/openclaw-send.sh` parses them and must match, and `_escape_stdout_delimiters` must learn the new markers,
- CLI exit codes — extend `SKILL.md`'s "Exit code conventions" table.

## PR / commit checklist

- [ ] Commits are atomic (as small as possible), semantic, and scoped
- [ ] No secrets in diff
- [ ] Tests added/updated for changed behavior
- [ ] `pytest` passes for touched areas
- [ ] `ruff check .` clean (or existing baseline unchanged)
- [ ] `mypy src` clean (or existing baseline unchanged)
- [ ] Docs/examples updated if config/CLI behavior changed
