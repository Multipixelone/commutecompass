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
| `prep.prep_minutes`                       | int (min)   | Route/event prep lead; separate from the alarm window's shared prep advance |
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
| `alarm.fallback_time`                     | "HH:MM"     | NYC base final deadline with window enabled; legacy latest wake when disabled; default `12:00` |
| `alarm.wake_lead_minutes`                 | int (min)   | Prep-only/window-disabled wake lead and corresponding nap cap; not the window's prep advance |
| `alarm.window.prep_advance_minutes`       | int (min)   | Canonical shared calendar + fallback advance; 0–1440, default 0; subtract once from base final |
| `alarm.window.calendar_advance_minutes`   | int (min)   | Legacy input alias for `alarm.window.prep_advance_minutes`; not a second or calendar-only setting |

## Shared wake-window setting

Both advance spellings address **one value**. Effective TOML/JSON config output
uses `prep_advance_minutes`; `config set` through either spelling writes the
canonical key instead of leaving an independent override. If both keys exist in
a file, equal values are accepted and conflicting values are rejected on load
and by set/unset without rewriting the file; the configuration owner must repair
the conflicting source first. Do not add a second 15-minute buffer. On a
non-conflicting config, `config unset` through either spelling clears both keys;
reset likewise removes the setting and restores default 0.

With `alarm.window.enabled = true`, first ring is the effective final minus
`first_alarm_lead_minutes`; the effective final is healthy `leave_at` or
`alarm.fallback_time` minus the shared advance, with `deadline_lead_minutes = 0`.
Calendar and fallback windows compare effective finals. Noon fallback, advance
15 and first lead 60 therefore give first 10:45 / final 11:45; lights start 10:35
from the published first ring, never from another advance. At advance 0 the same
example gives lights 10:50 / first 11:00 / final noon.

`prep.prep_minutes` remains separate route/event prep. Manual `alarm wake --leave`
deadlines are not advanced, naps keep their existing caps, and disabling the
window retains legacy automatic-wake behavior. `alarm.window.enabled`,
`first_alarm_lead_minutes` and `deadline_lead_minutes` are not chat-editable keys
in this allowlist. The [infra integration guidance](../../../docs/alarm-design.md#infra-integration-guidance--separate-work-not-executed)
is separate, unexecuted owner work; Nix-managed values belong in their declarative
source, not an attempted edit of generated `/etc` configuration.

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
