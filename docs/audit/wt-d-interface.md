# WT-D — Normalize configuration failures at the CLI boundary

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-config-errors` / `fix/audit-config-errors`.
**Exclusive write scope:** `src/commutecompass/config.py`, `src/commutecompass/cli.py`,
`tests/test_config.py`, `tests/test_cli.py`, `tests/test_config_atomic.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## CFG-1 — P2 — invalid configuration bypasses exit 78

**Source:** `load_config`, `config.py:475-507`, opens/parses TOML and performs time,
section and environment conversions outside a normalized `ConfigError` boundary.
`cli.py:42-51` only maps `ConfigError`/`ValidationError` to `EXIT_CONFIG=78`.
The Click config path option (`:92-99`) does not require an existing file.

**Reproduced offline:** isolated `CliRunner` calls to `init-db` with a missing config,
malformed TOML, `99:99` time, or `notify=42` exit 1 with respectively
`FileNotFoundError`, `TOMLDecodeError`, `ValueError`, or `AttributeError`, rather than
78. They fail before DB or network activity. `int(TELEGRAM_CHAT_ID)` is another
conversion requiring a regression test.

## CFG-2 — P1 — existing validation diagnostics disclose secret input

**Confirmed by oracle offline probe:** an otherwise-valid synthetic config with a
sentinel-bearing invalid-scheme heartbeat URL exits 78 and prints the sentinel.
`cli.py:49-51` prints raw Pydantic `ValidationError`; the heartbeat validator at
`config.py:271-277` embeds the full URL in its own error message. This is a demonstrated
malformed-config disclosure path, not evidence of a real credential incident.

Sanitize existing validation diagnostics too: render safe field/error categories,
never raw input, validator exception messages or error context. Merely hiding
Pydantic `input_value` is insufficient because validator text already contains the
secret. Remove secret interpolation in the heartbeat validator itself. Add an
otherwise-valid synthetic-config CLI regression with an invalid URL sentinel.

### Fix boundary

Validate section mapping shapes and normalize expected file/parse/conversion failures
as safe `ConfigError`s. Preserve existing missing-environment diagnostics and CLI
exit constants. Do not catch every programming error at the top-level CLI: expected
user errors should be classified near the load boundary. Error messages may name
fields/files but must not echo credentials or entire input sections. Preserve valid
config loading, redaction, atomic config updates, and the set allowlist.

### Regression tests / acceptance

- The reproduced cases and invalid `TELEGRAM_CHAT_ID` exit 78 with a concise error,
  no traceback, and no command execution/DB creation/network access.
- Non-mapping TOML sections are rejected intentionally rather than crashing `.get`.
- Permission-denied/open errors become config errors; use mocked file operations.
- Secret sentinel values do not appear in any new diagnostic, including invalid
  secret-bearing sections. Use fake values, not real environment/credential files.
- Valid configs and existing `config set`/redaction/exit-code tests remain green.

**Focused command:** `pytest -q tests/test_config.py tests/test_config_atomic.py tests/test_cli.py`.
**Suggested commit:** `fix(config): normalize load failures to config errors`.

## Legacy audit disposition

UI-1 through UI-11 are resolved: current code/tests cover command error boundaries,
output framing and sanitization, config redaction/allowlisting/atomic writes, and
skill dispatch synchronization. UI-12 is withdrawn: the contributed sender's bash
implementation is intentional, not a porting requirement. The re-audit NOTIFY2
byte-count claim is also withdrawn. Notification logging and acknowledgement work
has a separate exclusive owner in [WT-G](wt-g-sensitive-logging.md); do not edit
`notify.py`, transport logging policy, formatting, or skill scripts in this lane.
