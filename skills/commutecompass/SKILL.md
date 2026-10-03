---
name: commutecompass
description: Plan and adjust NYC commutes from chat — preview today's digest, see or shift prep time for a specific event, run morning/poll on demand, view or tweak safe config fields. Use when the user asks about their commute, calendar-driven travel times, leave or prep times, or NYC transit alerts affecting their day; or when they want to change planning behavior like prep buffer, quiet hours, or morning run time.
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins: [commutecompass-skill]
    emoji: "🧭"
    homepage: https://github.com/Multipixelone/commutecompass
---

# commutecompass — agent guide

You're being asked about the user's NYC commute planner. It already runs on a
schedule (morning digest at ~06:00, poll loop every minute) and pushes Telegram
messages through OpenClaw. Your job here is to handle on-demand questions and
adjustments.

All scripts shell out to `commutecompass-skill`, a wrapper installed by the
NixOS module that sources the secrets env file and points at the right
`config.toml`. The wrapper expects the invoking user to be a member of the
`commutecompass` group; the systemd timers continue to run as the
`commutecompass` user with EnvironmentFile= injected directly. Scripts print
to stdout; relay that stdout back to the user.

## Dispatch

| User intent                                                              | Script                                            |
|--------------------------------------------------------------------------|---------------------------------------------------|
| "what's on for today?" / "what's my next event?"                         | `scripts/digest.sh`                               |
| "where am I right now according to HA?"                                  | `scripts/where.sh`                                |
| "replan event X" / "what's the route for X?"                             | `scripts/plan-event.sh <selector>`                |
| "what if I were leaving from <address>?"                                 | `scripts/plan-event.sh <selector> --from "<addr>"` (preview only) |
| "I need 45 min to shower before <event>" / "shift prep earlier by N min" | `scripts/adjust.sh <selector> --add-prep 45`      |
| "undo that adjust" / "revert the last shift"                             | `scripts/undo.sh [<selector>]`                    |
| "snooze my prep ping 10 min" / "skip the next prep ping"                 | `scripts/snooze.sh <selector> --minutes 10` *or* `--skip` |
| "mute pings for event X" / "mute everything today"                       | `scripts/mute.sh <selector>` *or* `scripts/mute.sh --today` |
| "unmute event X"                                                         | `scripts/unmute.sh <selector>`                    |
| "what alerts are hitting my commute today?"                              | `scripts/mta-alerts.sh`                           |
| "is my train running late?" / "any real-time delays on my commute?"      | `scripts/realtime.sh`                             |
| "send me today's digest again" / "force-run morning"                     | `scripts/morning.sh`                              |
| "run a poll cycle now" / "check alerts now"                              | `scripts/poll.sh`                                 |
| "what time will my alarm be tomorrow?" / "preview tomorrow's wake time"  | `scripts/tomorrow.sh` (dry-run; no HA push)       |
| "are you alive?" / "send a test ping"                                    | `scripts/test-notify.sh`                          |
| "what's my prep buffer set to?" / "show me my config"                    | `scripts/config-show.sh`                          |
| "set my prep buffer to 30 min"                                           | `scripts/config-set.sh <dotted.key> <value>`      |
| "turn off quiet hours" / "remove that override"                          | `scripts/config-unset.sh <dotted.key>`            |
| "reset all my config tweaks"                                             | `scripts/config-reset.sh --yes`                   |
| "why didn't I get my morning ping?" / "show me the current state"        | `scripts/status.sh` (text) or `scripts/status.sh --json` |

### Real-time diagnostic interpretation

`realtime` reports each boarding leg as `observed`, `unavailable`, `unmatched`,
`not_applicable`, `cancelled`, or `skipped`. Zero padding is **not** evidence of
on-time service. Feed fetch/parse failures exit **75**, including partial failures;
successful observations from other feeds/plans are still printed. Disabled mode
continues to report disabled and exit 0. Stale/unsupported feeds report unavailable
without treating them as a successful on-time observation.

Alarm padding requires independently validated GTFS trip/service date, route,
direction, boarding platform and stop sequence, plus an explicit departure delay
and predicted time whose derived scheduled time equals the plan. Ordinary
Directions routes do not supply this identity and therefore safely report
unmatched (zero padding). Absolute feed departures may be shown as information,
never as a measured delay or as confirmation of the planned train/direction.
No LIRR branch mapping is assumed; ambiguous same-name stations are rejected.

Producer headers and supplied trip-update timestamps must be at most 5 minutes
old and at most 60 seconds ahead of the actual observation clock, independent of
the planned event time. A missing header is ineligible; a missing trip-update
timestamp uses the header. Differential/deleted updates are not applied; cancelled,
skipped and NO_DATA service cannot add padding. This command does not refresh
scheduled pings or establish a new arrival time.

Normal `poll` independently refreshes enabled realtime **before due-alarm
dispatch**, without HA or a new service alert. It considers persisted active
plans with a pending prep/leave alarm, a future event start, and a leave time
between 15 minutes overdue and 60 minutes ahead. It reuses the stored route
(no Directions replan), with the existing 60-second per-system feed cache;
freshness is measured against actual poll time. Only an `observed` padding
increase is applied, including increases below the unrelated 5-minute service
replan threshold. Unknown/unmatched/unavailable or smaller delays retain prior
safety padding: alarms are never postponed by this refresh.

Only the realtime-buffer delta advances saved prep/leave times, using UTC
elapsed arithmetic; weather, travel and manual offsets remain intact. Plan
and pending alarm updates share one SQLite write transaction with a stale-plan
compare guard. Pending rows retain IDs and retry counts; fired rows are untouched.
Newly urgent alarms clamp to now and can dispatch in the same poll; already-due
alarms stay due. Repeated observations do not accumulate padding or recreate
alarms. Existing quiet-hours, mute and bounded-send-retry rules still apply.

**Capability limitation:** ordinary Directions plans still lack independently
validated static-GTFS trip correspondence, so this refresh safely leaves them
unmatched. Automatic measured-delay alarm adjustment is not fully functional for
those routes until that separate correspondence capability exists. Diagnostic
queries remain read-only and cannot supply or fabricate that identity.

Feed fetching has a five-second monotonic admission budget per system/feed set.
Each feed gets at most two attempts with no retry backoff; each attempt divides
min(2 seconds, remaining budget) across HTTPX timeout phases. Exhaustion skips
later feeds and preserves partial observations with failure status (exit 75).
HTTPX phase/inactivity timeouts are not a hard streaming/parsing wall-time limit.

## Selectors

Every event-scoped command (`plan-event`, `adjust`, `snooze`, `mute`,
`unmute`, `undo`) accepts a `SELECTOR` instead of a raw event ID. The
digest no longer prints the calendar event ID — use whichever of these
is most natural:

| Form              | Meaning                                                |
|-------------------|--------------------------------------------------------|
| `next`            | The soonest plan whose start time is after now.        |
| `today:N`         | 1-indexed pick from today's plans (`today:1`, `today:2`…). |
| `[8 hex chars]`   | An 8+ char hex prefix of the Google Calendar event id, if you already have one (e.g. from a prior `plan-event` call). Not shown in the digest. |
| Full event ID     | Exact match against the Google Calendar event id.      |
| Title fragment    | Fuzzy match against today's titles (rapidfuzz).        |

Failure modes the CLI exits with:

- `EXIT_NOT_FOUND` (65) — the selector matched nothing.
- `EXIT_UNRESOLVED` (66) — the selector was ambiguous (two events share an
  ID prefix, or two titles fuzz-match equally). Ask the user which one.

## `config set` allowlist

Only the keys listed in `references/config-allowlist.md` are editable. The
command will refuse anything else. Do not attempt to write secrets, paths,
calendar IDs, MTA URLs, or LLM endpoints — those live outside the allowlist on
purpose.

## Notes

- `morning` and `poll` mutate state (fire pings, send messages). Confirm with
  the user before running them on demand if the cause for the user's request is
  unclear.
- `digest-preview`, `where`, `plan-event` (without `--from`), `config-show`,
  `mta-alerts`, and `realtime` are pure reads — invoke freely. `plan-event --from <addr>`
  is also a read (preview only; never saves).
- `adjust` only shifts `prep_at`. The `leave_at` is governed by route+event
  start and can't be moved without a replan. If the user wants to leave
  earlier/later, that requires a different change (calendar edit or route
  override).
- `adjust` accepts `--idempotency-key <opaque>`. If you (the agent) might
  retry the same request, pass a stable key (e.g. an upstream correlation id,
  or `<event_id>:<add_prep>:<YYYYMMDD>`) so duplicate retries no-op rather
  than stacking the offset.
- `undo` reverts one adjust at a time and walks history on repeat calls. Each
  call restores `prep_at` to the exact value captured before that adjust.
- `mute` is forward-looking: a ping that has already fired stays fired. To
  silence an already-sent prep ping, use `snooze --skip` *before* it fires.
- `mute --today` cancels pending pings and re-mutes them until end-of-day;
  the next morning's digest will repopulate as normal.
- `snooze` only operates on **prep** pings. Leave pings are operationally
  critical and intentionally non-snoozable from chat.
- See `references/examples.md` for end-to-end chat → command mappings.

## Exit code conventions

Commands use the following exit codes so an agent caller can distinguish
failure modes without parsing log output:

| Code | Meaning                                                    |
|------|------------------------------------------------------------|
| 0    | Success.                                                   |
| 64   | Usage / bad arguments (Click already uses 2; we use 64 for `config set` rejects). |
| 65   | Subject not found (no plan / event / location row).        |
| 66   | Data could not be resolved (no current location, no route, prep/leave missing). |
| 75   | Transient failure: job lock held by another process; retry next cycle. |
| 78   | Config error (missing env var, malformed TOML).            |
