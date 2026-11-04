# WT-D — Messages, config CLI, OpenClaw glue

**Branch / worktree:** `audit/wt-d-interface`
**Write scope (exclusive):** `src/commutecompass/{format,config}.py`, `contrib/openclaw-send.sh`,
`skills/commutecompass/**`, `tests/test_format.py`, `tests/test_config*.py`, new
`tests/test_skill_dispatch.py`, `tests/test_openclaw_send.py`.
Do not edit `notify.py` unless strictly needed for UI-4 (no other worktree owns it).
**Validation:** `pytest -q` on the files above, `ruff check .`, `mypy src`, `bash -n contrib/openclaw-send.sh`.
**Rules:** AGENTS.md gotchas 2, 4, 7 and the "When changing behavior" list (keep SKILL.md and
`references/config-allowlist.md` in sync).

## UI-1 — P1 — alert routes not sanitized
- **Where:** `format.py:249-252`. `affected_routes` are `escape_md`'d but not `_sanitize_text`'d
  (feed-controlled text; bidi/control chars reach Telegram). Sanitize each route first. Test with
  a route containing `\u202e` and a control char.

## UI-2 — P1 — route detail fields not sanitized
- **Where:** `format.py:438-460` `_route_summary_detailed`. Wrap `leg.line`, stop names,
  headsigns with `_sanitize_text` before `escape_md`. Test.

## UI-5 — P2 — send failure logs message body
- **Where:** `contrib/openclaw-send.sh:53-57` prints the first 80 chars of the message (event
  titles/locations) to stderr/journal. Log message ordinal and byte length only.

## UI-3 — P2 — silent framing errors in openclaw-send.sh
- **Where:** `openclaw-send.sh:61-90`. Unframed output is dropped silently, a nested START
  resets the buffer, a stray END is ignored. Warn on stderr for each case and exit non-zero at
  the end if any framing error occurred (still send well-formed messages).

## UI-4 — P2 — no end-to-end framing test
- Add `tests/test_openclaw_send.py`: run `StdoutNotifier` output (multiple messages, a body line
  equal to the delimiter) through the script with a fake `openclaw` on `PATH` that records argv;
  assert exact messages delivered. Skip if `bash` missing.

## UI-9 — P2 — `config set` can write an unloadable config
- **Where:** `config.py:549-554` `_coerce_int` (no bounds), `update_config_field`.
  `config set scheduling.poll_interval_seconds 0` writes a file that later fails validation.
- **Fix:** after applying the edit in memory, validate the whole document with the same pydantic
  model used by `load_config`; on failure, do not write and exit `EXIT_CONFIG` with the error.
- **Test:** invalid value → file unchanged, error raised.

## UI-10 — P2 — non-atomic config writes
- **Where:** `config.py:689,720` (`update_config_field`, `delete_config_field`) use `open(path,"w")`.
- **Fix:** write to a temp file in the same dir, `fsync`, `os.replace`, preserve original mode.
  Test that mode is preserved and a failure mid-write leaves the original intact.

## UI-6 — P2 — redaction relies on keyword heuristic
- **Where:** `config.py:521-534` `redact_for_display`. Add an explicit set of secret field paths
  plus extend keywords (`password`, `credential`, `bearer`). Test every secret field in `Config`
  is redacted.

## UI-7 — P2 — skill dispatch drift test
- `tests/test_skill_dispatch.py`: each script referenced by `skills/commutecompass/SKILL.md`
  exists and is executable; each wrapper's CLI subcommand exists in the click group.

## UI-8 — P2 — allowlist doc sync test
- Assert `CONFIG_SET_ALLOWLIST` keys == keys listed in
  `skills/commutecompass/references/config-allowlist.md`.

## UI-11 — P3 — allowlisted keys not read at runtime
- `scheduling.morning_run_time` / `scheduling.poll_interval_seconds` are settable but timers come
  from Nix. Default decision: keep them, but make `config set` print a warning that the systemd
  timer (Nix module) must also change, and note it in the allowlist doc.

## UI-12 — P3 — WONTFIX
- Bash shebangs in `contrib/` and skill scripts are intentional (portable OpenClaw hosts); the
  user's fish preference applies to personal shell scripts, not shipped glue. No change.
