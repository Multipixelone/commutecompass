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
| "send me today's digest again" / "force-run morning"                     | `scripts/morning.sh`                              |
| "run a poll cycle now" / "check alerts now"                              | `scripts/poll.sh`                                 |
| "what time will my alarm be tomorrow?" / "preview tomorrow's wake time"  | `scripts/tomorrow.sh` (dry-run; no HA push)       |
| "are you alive?" / "send a test ping"                                    | `scripts/test-notify.sh`                          |
| "what's my prep buffer set to?" / "show me my config"                    | `scripts/config-show.sh`                          |
| "set my prep buffer to 30 min"                                           | `scripts/config-set.sh <dotted.key> <value>`      |
| "turn off quiet hours" / "remove that override"                          | `scripts/config-unset.sh <dotted.key>`            |
| "reset all my config tweaks"                                             | `scripts/config-reset.sh --yes`                   |
| "why didn't I get my morning ping?" / "show me the current state"        | `scripts/status.sh` (text) or `scripts/status.sh --json` |

## Selectors

Every event-scoped command (`plan-event`, `adjust`, `snooze`, `mute`,
`unmute`, `undo`) accepts a `SELECTOR` instead of a raw event ID. The
digest now prints an `[8-char-id]` token for every plan, but you usually
don't need to quote it — use whichever of these is most natural:

| Form              | Meaning                                                |
|-------------------|--------------------------------------------------------|
| `next`            | The soonest plan whose start time is after now.        |
| `today:N`         | 1-indexed pick from today's plans (`today:1`, `today:2`…). |
| `[8 hex chars]`   | The short ID printed in the digest (e.g. `a1b2c3d4`).  |
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
  and `mta-alerts` are pure reads — invoke freely. `plan-event --from <addr>`
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
