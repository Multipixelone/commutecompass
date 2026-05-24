# Chat → command examples

Concrete mappings the agent can pattern-match against. All commands assume
`COMMUTECOMPASS_CONFIG` is exported.

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
# 1. find the event id matching "3pm"
scripts/digest.sh
# 2. shift its prep_at 45 minutes earlier
scripts/adjust.sh <event_id> --add-prep 45
```

> push my standup prep 10 min later, I want to sleep in
```
scripts/adjust.sh <event_id> --add-prep -10
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
# allowlist doesn't include "unset"; pick a 1-min window so suppression is
# effectively off, or refuse and tell the user to edit config.toml.
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
