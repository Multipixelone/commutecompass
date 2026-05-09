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
```

## Architecture map (quick)

- `config.py`: TOML + env loading into `Config`
- `calendar_client.py`: OAuth/token + Google Calendar fetch
- `resolver.py`: raw location -> cache/venues/geocode/LLM pipeline
- `planner.py`: event -> resolved location -> route -> leave/prep times
- `store.py`: SQLite state (plans, pings, geocode cache, alerts_seen)
- `format.py`: Telegram MarkdownV2-safe messages
- `notify.py`: Telegram API send
- `jobs/morning.py`: daily planning + digest
- `jobs/poll.py`: due pings + alert-triggered replanning

## Known gotchas

1. **OAuth env format**
   - `GOOGLE_OAUTH_CLIENT_SECRET` is expected to be a **JSON string** (client config), not just the raw secret token.

2. **Telegram MarkdownV2 is strict**
   - Escape user/content fields with `escape_md()`.
   - Literal markdown-sensitive punctuation in formatted templates (e.g. `(` and `)`) must also be escaped when needed.

3. **Non-actionable locations**
   - Placeholder locations like “Location available once RSVP’d” should short-circuit in resolver (no unnecessary LLM call).

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
- scheduling semantics in jobs.

## PR / commit checklist

- [ ] Commits are atomic (as small as possible), semantic, and scoped
- [ ] No secrets in diff
- [ ] Tests added/updated for changed behavior
- [ ] `pytest` passes for touched areas
- [ ] `ruff check .` clean (or existing baseline unchanged)
- [ ] `mypy src` clean (or existing baseline unchanged)
- [ ] Docs/examples updated if config/CLI behavior changed
