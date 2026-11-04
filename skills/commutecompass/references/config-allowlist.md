# `config set` allowlist

`commutecompass config set KEY VALUE` only accepts the keys below. Anything
else (paths, credentials, calendar IDs, MTA URLs, LLM endpoints) is
intentionally read-only via the skill and must be changed by editing
`config.toml` directly on the host.

The two scheduling cadence keys remain editable for compatibility, but are not
read at runtime. `config set` prints a warning: change the systemd timer in the
Nix module (or the cron entry) as well to change when the job actually runs.

| Key                                       | Type        | Notes                                           |
|-------------------------------------------|-------------|-------------------------------------------------|
| `prep.prep_minutes`                       | int (min)   | Default prep buffer for every event             |
| `prep.safety_buffer_minutes`              | int (min)   | Extra slack added on top of route duration      |
| `scheduling.morning_run_time`             | "HH:MM"     | Advisory; update the Nix/systemd timer too      |
| `scheduling.poll_interval_seconds`        | int (sec)   | Advisory; update the Nix/systemd timer too      |
| `scheduling.quiet_hours_start`            | "HH:MM"     | Begin suppressing prep/alert pings              |
| `scheduling.quiet_hours_end`              | "HH:MM"     | End of quiet-hours window                       |
| `notify.mode`                             | enum        | `"stdout"` or `"telegram"`                      |
| `home_assistant.alarm.enabled`            | bool        | Toggle the additive HA loud-alarm channel       |
| `home_assistant.tomorrow.enabled`         | bool        | Toggle the pull-model "tomorrow alarm" push     |
| `home_assistant.replan_window_minutes`    | int (min)   | Window before leave_at in which to replan       |
| `home_assistant.max_age_minutes`          | int (min)   | Max acceptable age of an HA location reading    |
| `realtime.enabled`                        | bool        | Toggle real-time GTFS-RT departure delay buffer |
| `realtime.max_buffer_minutes`             | int (min)   | Cap on minutes a live delay can add to leave time |
| `alarm.fallback_time`                     | "HH:MM"     | Latest wake time (NYC); used when no events      |
| `alarm.wake_lead_minutes`                 | int (min)   | Wake this many minutes before the first prep     |

## How to revert

Two skill commands clear chat-tweakable overrides without editing TOML by
hand:

- `commutecompass config unset KEY` — remove a single allowlisted key so its
  schema default takes over. Example: clearing
  `scheduling.quiet_hours_start` *and* `..._end` turns quiet hours off
  cleanly. Refuses non-allowlisted keys with the same surface as
  `config set`.
- `commutecompass config reset` — print every allowlisted override that is
  currently set and exit non-zero (preview only). Re-run with `--yes` to
  actually remove all of them. Non-allowlisted blocks
  (`[origin]`, `[paths]`, `[[calendars]]`, MTA URLs, secrets) are never
  touched.

## Refusal examples

If the user asks to change something not on this list, refuse and explain
which key would have done it (if any), or point them at the host-side TOML.
Examples:

- "change my home address" → not on allowlist; edit `[origin]` in `config.toml`.
- "add a new calendar" → not on allowlist; edit `[[calendars]]` in `config.toml`.
- "rotate the telegram bot token" → not on allowlist; rotate via env var, not
  TOML.
- "set per-calendar prep minutes" → not yet supported via the skill — the
  data model treats prep as global. Adjust via `prep.prep_minutes`, or use
  per-event `adjust` from chat.
