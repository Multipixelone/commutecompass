# `config set` allowlist

`commutecompass config set KEY VALUE` only accepts the keys below. Anything
else (paths, credentials, calendar IDs, MTA URLs, LLM endpoints, home_assistant
settings) is intentionally read-only via the skill and must be changed by
editing `config.toml` directly on the host.

| Key                                 | Type        | Notes                                           |
|-------------------------------------|-------------|-------------------------------------------------|
| `prep.prep_minutes`                 | int (min)   | Default prep buffer for every event             |
| `prep.safety_buffer_minutes`        | int (min)   | Extra slack added on top of route duration      |
| `scheduling.morning_run_time`       | "HH:MM"     | When the morning digest fires (NYC local time)  |
| `scheduling.poll_interval_seconds`  | int (sec)   | Poll cadence; should match the cron interval    |
| `scheduling.quiet_hours_start`      | "HH:MM"     | Begin suppressing prep/alert pings              |
| `scheduling.quiet_hours_end`        | "HH:MM"     | End of quiet-hours window                       |
| `notify.mode`                       | enum        | `"stdout"` or `"telegram"`                      |

## Refusal examples

If the user asks to change something not on this list, refuse and explain
which key would have done it (if any), or point them at the host-side TOML.
Examples:

- "change my home address" → not on allowlist; edit `[origin]` in `config.toml`.
- "add a new calendar" → not on allowlist; edit `[[calendars]]` in `config.toml`.
- "rotate the telegram bot token" → not on allowlist; rotate via env var, not
  TOML.
