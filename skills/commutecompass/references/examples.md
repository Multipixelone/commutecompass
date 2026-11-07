# Chat → command examples

Concrete mappings the agent can pattern-match against. All scripts shell out
to `commutecompass-skill`, which the NixOS module installs on PATH for users
listed in `services.commutecompass.skill.users`. No env or config-path
preamble required.

## Selectors

Every event-scoped command accepts a flexible `SELECTOR` argument:

```
next                       # the soonest plan after now
today:1, today:2, ...      # 1-indexed pick from today's plans
a1b2c3d4                   # 8-char id prefix (shown in the digest)
"3pm show"                 # fuzzy title match (rapidfuzz)
```

> what's the next thing on?
```
scripts/digest.sh
# or, for a single event: scripts/plan-event.sh next
```

> shift my 3pm show prep earlier by 30 min
```
scripts/adjust.sh "3pm show" --add-prep 30
```

## Read-only queries

> what's on for today?
```
scripts/digest.sh
```

> what's my next event?
```
scripts/digest.sh
# pick the earliest plan whose start time is in the future
```

> where am I right now?
```
scripts/where.sh
```

> what would the route be for event abc123?
```
scripts/plan-event.sh abc123
```

> what if I were leaving from Brooklyn Bridge instead?
```
# preview only — does NOT overwrite the stored plan
scripts/plan-event.sh next --from "Brooklyn Bridge"
```

> what alerts are affecting my commute today?
```
scripts/mta-alerts.sh
```

> what's my prep buffer set to?
```
scripts/config-show.sh | grep prep_minutes
```

> show me my whole config
```
scripts/config-show.sh
```

## Adjustments

> I need 45 minutes to shower before my 3pm
```
scripts/adjust.sh "3pm" --add-prep 45
# (selector falls through to fuzzy title match)
```

> push my standup prep 10 min later, I want to sleep in
```
scripts/adjust.sh standup --add-prep -10
```

> undo that
```
scripts/undo.sh
# or scoped: scripts/undo.sh standup
```

> change my default prep buffer to 30 minutes
```
scripts/config-set.sh prep.prep_minutes 30
```

> shift my morning digest to 6:30
```
scripts/config-set.sh scheduling.morning_run_time 06:30
```

> turn off quiet hours
```
scripts/config-unset.sh scheduling.quiet_hours_start
scripts/config-unset.sh scheduling.quiet_hours_end
```

> reset all my config tweaks
```
# dry-run first (no --yes lists what would change and refuses)
scripts/config-reset.sh
scripts/config-reset.sh --yes
```

## Manual wake deadlines

> wake me at 10:00 with 3 snoozes

```sh
scripts/alarm-wake.sh --leave 10:00 --snoozes 3
scripts/alarm-status.sh
```

Report first ring **09:40:30**, final deadline **10:00:00**, interval
**6 minutes 30 seconds (390 seconds)**, and maximum **3** accepted snoozes,
subject to the command's calendar-conflict and publication results. The requested
time is the final deadline. Delayed actions may leave fewer full snoozes.

> final alarm at 10 tomorrow, no snoozes

```sh
# Resolve "tomorrow" to the user's America/New_York date, then:
scripts/alarm-wake.sh --leave 10:00 --date YYYY-MM-DD --snoozes 0
```

This starts directly in the desktop-dismissal-only final phase at 10:00:00.

> I need to leave at 9

```sh
scripts/alarm-wake.sh --leave 09:00
```

No count was requested: preserve the configured wake lead and snooze behavior.
Use `--force` only after an explicit instruction to override an earlier calendar
alarm. If a count-based first ring is already past, ask before retrying with
`--allow-late`; explain that the final deadline remains unchanged and some
snooze opportunities have been lost. A manual count is never a global config edit.

> cancel that manual wake alarm

```sh
scripts/alarm-wake.sh --clear
```

This clears only the manual override and republishes the ordinary schedule.

## Snooze, skip, mute

> snooze my next prep ping by 10 min
```
scripts/snooze.sh next --minutes 10
```

> skip the prep ping for the matinee
```
scripts/snooze.sh matinee --skip
```

> mute everything today, I'm staying in
```
scripts/mute.sh --today
```

> mute my evening rehearsal forever
```
scripts/mute.sh "evening rehearsal"
```

> unmute that
```
scripts/unmute.sh "evening rehearsal"
```

## Live actions (confirm first)

> send me today's digest right now
```
scripts/morning.sh
```

> are you alive?
```
scripts/test-notify.sh
```

> run a poll cycle now
```
scripts/poll.sh
```
