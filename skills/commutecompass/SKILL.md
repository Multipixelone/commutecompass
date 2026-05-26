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
| "replan event X" / "what's the route for X?"                             | `scripts/plan-event.sh <event_id>`                |
| "I need 45 min to shower before <event>" / "shift prep earlier by N min" | `scripts/digest.sh` (find id) → `scripts/adjust.sh <event_id> --add-prep 45` |
| "send me today's digest again" / "force-run morning"                     | `scripts/morning.sh`                              |
| "run a poll cycle now" / "check alerts now"                              | `scripts/poll.sh`                                 |
| "what time will my alarm be tomorrow?" / "preview tomorrow's wake time"  | `scripts/tomorrow.sh` (dry-run; no HA push)       |
| "are you alive?" / "send a test ping"                                    | `scripts/test-notify.sh`                          |
| "what's my prep buffer set to?" / "show me my config"                    | `scripts/config-show.sh`                          |
| "set my prep buffer to 30 min" / "change quiet hours to ..."             | `scripts/config-set.sh <dotted.key> <value>`      |

## Resolving an event ID for `adjust`

`adjust` needs the Google Calendar event ID. It's not in the digest text. To
get it:

1. Run `scripts/digest.sh` (`commutecompass-skill digest-preview`).
2. The DB-cached plans match what's shown in the digest. Use the user's
   description (event title, time) to pick the right one. If the digest is
   ambiguous, ask the user which event they mean.
3. If you don't have the ID directly visible, ask the user to forward you the
   leave-ping for that event (the ID isn't currently surfaced in the digest;
   consider asking before guessing).

## `config set` allowlist

Only the keys listed in `references/config-allowlist.md` are editable. The
command will refuse anything else. Do not attempt to write secrets, paths,
calendar IDs, MTA URLs, or LLM endpoints — those live outside the allowlist on
purpose.

## Notes

- `morning` and `poll` mutate state (fire pings, send messages). Confirm with
  the user before running them on demand if the cause for the user's request is
  unclear.
- `digest-preview`, `where`, `plan-event`, and `config-show` are pure reads —
  invoke freely.
- `adjust` only shifts `prep_at`. The `leave_at` is governed by route+event
  start and can't be moved without a replan. If the user wants to leave
  earlier/later, that requires a different change (calendar edit or route
  override).
- `adjust` accepts `--idempotency-key <opaque>`. If you (the agent) might
  retry the same request, pass a stable key (e.g. an upstream correlation id,
  or `<event_id>:<add_prep>:<YYYYMMDD>`) so duplicate retries no-op rather
  than stacking the offset.
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
