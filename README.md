<h1 align="center">commutecompass</h1>
<div align="center">

[![Build](https://img.shields.io/github/actions/workflow/status/Multipixelone/commutecompass/ci.yml?style=for-the-badge&logo=github&label=build&color=a6e3a1&labelColor=313244&logoColor=cdd6f4)](https://github.com/Multipixelone/commutecompass/actions)
[![License](https://img.shields.io/github/license/Multipixelone/commutecompass?style=for-the-badge&logo=creativecommons&color=b4befe&labelColor=313244&logoColor=cdd6f4)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12+-fab387?style=for-the-badge&logo=python&labelColor=313244&logoColor=cdd6f4)
![Nix](https://img.shields.io/badge/nix-flakes-89b4fa?style=for-the-badge&logo=nixos&labelColor=313244&logoColor=cdd6f4)

</div>

A self-hosted [Python](https://www.python.org/) service that pulls events from Google Calendar, computes optimal departure times factoring NYC multimodal transit, and pushes daily digests and per-event notifications to Telegram.

> note: vibe coded to high heck and back. this is exclusively for me to have less adhd time blindness :)

## Commands

- [`oauth`](./src/commutecompass/cli.py) — interactive Google Calendar OAuth setup
- [`init-db`](./src/commutecompass/store.py) — initialize the SQLite schema
- [`morning`](./src/commutecompass/jobs/) — run the morning digest job
- [`poll`](./src/commutecompass/jobs/) — run the per-minute poll loop
- [`tomorrow`](./src/commutecompass/jobs/) — push tomorrow's earliest prep time to the configured HA script (pull-model alarm)
- [`plan`](./src/commutecompass/planner.py) — replan a single event (debug)
- [`digest-preview`](./src/commutecompass/cli.py) — print today's digest from cache without sending
- [`status`](./src/commutecompass/cli.py) — show next event, pending pings, and job heartbeats (`--json` for machine output)
- [`adjust EVENT_ID --add-prep N`](./src/commutecompass/cli.py) — shift a plan's prep time by N minutes
- [`snooze`](./src/commutecompass/cli.py) / [`mute`](./src/commutecompass/cli.py) / [`unmute`](./src/commutecompass/cli.py) / [`undo`](./src/commutecompass/cli.py) — adjust or suppress an event's pings
- [`config show`](./src/commutecompass/cli.py) / [`config set KEY VALUE`](./src/commutecompass/cli.py) / [`config unset KEY`](./src/commutecompass/cli.py) / [`config reset`](./src/commutecompass/cli.py) — view or edit allowlisted config fields
- [`geocode-cache`](./src/commutecompass/cli.py) — inspect or clear the geocode cache
- [`mta-alerts`](./src/commutecompass/cli.py) — print current MTA alerts
- [`realtime`](./src/commutecompass/cli.py) — read-only boarding-delay diagnostics for saved plans
- [`test-notify`](./src/commutecompass/notify.py) — emit a test message via the configured notifier
- [`where`](./src/commutecompass/cli.py) — print the latest stored current location
- [`alarm`](./src/commutecompass/cli.py) — wake and nap alarm controls: `publish`, `status`, `wake`, `nap`, `skip`, `on`, `off`, `daemon`
- [`alarm wake --leave HH:MM [--date YYYY-MM-DD] [--force]`](./src/commutecompass/cli.py) — persist a single-use NYC departure-anchored wake window; `alarm wake --clear` removes the override and republishes the ordinary schedule (also available through `commutecompass-skill`)
- [`gtfs-refresh`](./src/commutecompass/cli.py) — download static subway GTFS and rebuild the realtime identity index
- [`bot`](./src/commutecompass/cli.py) — Telegram bot mode (stub)

## Configuration

See [`examples/config.toml`](./examples/) and [`examples/env.example`](./examples/) for the full configuration schema. Architecture and implementation notes live in [`AGENTS.md`](./AGENTS.md).

## Real-time delays: capability and limitations

Enable `[realtime].enabled` to query GTFS-RT trip updates. Automatic padding
requires a **fresh, matched, measured departure delay**: independently validated
GTFS trip/service date, route, direction, boarding platform and stop sequence,
plus explicit departure delay/time agreeing with the saved scheduled departure.
Producer observations must be no older than five minutes or more than 60 seconds
ahead of the observation clock. Nearest trains and fuzzy station names cannot
establish this correspondence.

**Normal Directions plans currently lack validated GTFS trip mapping, so
automatic padding is unsupported for those plans** and safely remains zero.
This is not a fully complete automatic-delay feature; no static GTFS ingestion
or LIRR branch mapping is provided. Feed departures can be informational only.

`commutecompass realtime` reports `observed`, `unmatched`, `unavailable`,
`not_applicable`, `cancelled`, or `skipped`. Unknown is not on-time: only an
`observed` measurement can establish zero delay. Fetch/parse failures, including
partial failures, return `EXIT_TRANSIENT` (75), while retaining usable results.
The diagnostic command never changes alarms.

Normal `poll` refreshes enabled realtime before due-ping dispatch for active
saved plans with pending prep/leave pings and `leave_at` from 15 minutes overdue
to 60 minutes ahead. Only increased fresh observed buffers advance alarms;
unknown, failed or smaller observations never postpone them. Updates preserve
pending IDs, retry counts and manual offsets, leave fired pings untouched, and
clamp newly urgent pings to now. Ordinary Directions plans remain unmatched.

Fetching uses a five-second monotonic admission budget per system/feed set,
with at most two attempts per feed and no retry backoff. Each request divides
the smaller of two seconds or the remaining budget across HTTPX timeout phases.
Later feeds are skipped on exhaustion; partial observations and failure status
are retained. This bounds new I/O admission, not slow streaming/parsing wall
time (HTTPX timeouts are phase/inactivity limits). Results cache for 60 seconds.

See [real-time follow-up work](./docs/realtime-followups.md) for proposed next
steps, priorities, and acceptance criteria for the remaining integration gaps.

## Machine-readable trip timing

Run `commutecompass status --json`, or the NixOS-installed
`commutecompass-skill status --json`, to read persisted plans without re-routing.
Each entry in `plans` includes these additive fields alongside `event_id`,
`title`, `start`, `leave_at`, `prep_at`, `error`, and `resolved_source`:

```json
{
  "leave_at": "2026-10-03T13:36:08-04:00",
  "depart_at": "2026-10-03T13:49:08-04:00",
  "travel_minutes": 51.583333333333336,
  "arrive_at": "2026-10-03T14:40:43-04:00",
  "leave_buffer_minutes": 13
}
```

`travel_minutes` is the selected route's door-to-door duration, preserving
fractional minutes. When both route timestamps are known, it is their elapsed
seconds divided by 60; otherwise it uses that alternative's complete provider
leg durations. It includes walking, waiting, transit, and final walking for a
transit trip. It excludes prep and scheduling buffers. An album picker should
use it directly, rather than infer duration from event or alarm times.

`depart_at` and `arrive_at` are the route's door-to-door departure and arrival,
expressed as ISO 8601 timestamps with timezone offsets. For a fresh timed route:

- `arrive_at - depart_at = travel_minutes` (compare elapsed minutes).
- `leave_at = depart_at - leave_buffer_minutes`.
- `prep_at = leave_at - prep_minutes`.

`leave_at` is the earlier buffered alarm, not the route departure. The persisted
`leave_buffer_minutes` is the safety + weather + realtime buffer used when
planning; it excludes prep. The example's route still takes 51m 35s even though
its leave alarm is 13 minutes earlier. Scheduled transit can naturally arrive
before the event; alarms now respect its actual departure instead of working
backwards from event start and potentially missing the selected train.

Unknown route timing is `null`; all three route fields are `null` without a
route. In particular, Google can return a cycling/driving/walking duration
without scheduled timestamps: `travel_minutes` is then available while
`depart_at` and `arrive_at` remain `null`, even with `error: null`. Missing
provider times are never replaced with processing time or event start. Without
a current departure schedule, the alarm uses
`event start - travel duration - scheduling buffer`.

Cached routes still supply approximate duration, but their historical departure
and arrival are suppressed as `null` and never anchor a new alarm. Coarse
fallback estimates retain their estimated timestamps; all fallback timing is
best-effort. Existing plans require no database migration: their buffer is
`null` if not originally persisted, and their saved `leave_at` is unchanged
until normal replanning replaces it. `status` does not repair or re-route old
plans. `plan` and the `plan-event.sh` skill script provide text output only.

## OpenClaw integration

commutecompass ships an [OpenClaw](https://openclaw.ai) skill at [`skills/commutecompass/`](./skills/commutecompass/) so you can interact with it from chat — "what's on for today?", "shift my next event prep 45 min earlier", "set quiet hours to 23:00".

OpenClaw also owns the Telegram bot. Set `[notify].mode = "stdout"` in `config.toml` (the default in `examples/config.toml`) and commutecompass will emit each would-be Telegram message to stdout wrapped in delimiters. Pipe through [`contrib/openclaw-send.sh`](./contrib/openclaw-send.sh) to forward each message to `openclaw message send`:

```cron
0 6 * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
           commutecompass morning | \
           OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh

* * * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
           commutecompass poll | \
           OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh
```

Point your OpenClaw instance at `skills/commutecompass/`, and the chat commands are live. The scripts shell out to `commutecompass-skill`, a wrapper the NixOS module installs when `services.commutecompass.skill.users` is set; it sources the secrets env file and points at `/etc/commutecompass/config.toml`, so the calling session needs no preamble. Outside NixOS, ship an equivalent wrapper on PATH. The legacy direct-Telegram path is still available via `[notify].mode = "telegram"` if you'd rather not depend on OpenClaw.

**Skill-user trust boundary:** every user listed in
`services.commutecompass.skill.users` joins the service group and can read the
**entire** `services.commutecompass.environmentFile` (which must be group-readable,
e.g. mode `0440`). This exposes all configured API keys, Telegram bot tokens and
Home Assistant tokens, not merely the credentials needed for a particular skill
command. Add only users trusted with every service secret; the default shared-file
behavior is unchanged.

## Already at the destination

With Home Assistant location enabled, `poll` defers prep and leave reminders
(including their additive HA alarms) while a recent, accurate GPS fix confirms
you are already at the event's resolved destination. This also skips
location-driven replanning and its update message, avoiding a redundant
one-minute estimated commute between shifts at the same venue.

Confirmation requires a fix no older than five minutes (or the configured
location age limit, if shorter), with **distance to destination + GPS accuracy
≤ 100 metres**. Unknown accuracy, stale or untrustworthy timestamps, and
unresolved destinations do not suppress reminders. A short estimated route alone
is not arrival evidence.

Deferred reminders remain pending without changing their deadlines or retry
counts. They become eligible again if you leave or the location evidence expires
before the event starts; once the event starts, obsolete prep/leave reminders
are consumed silently rather than sent late. Morning planning still schedules
reminders normally. This is not an all-notifications mute: daily digests, MTA
service updates, manual queries, and tomorrow/wake-alarm publishing are unchanged.

## Overdue reminders

When prep and departure reminders are both overdue, **leave wins**: the obsolete
prep reminder is retired instead of sending “start getting ready” after “leave
now.” A prep reminder still fires normally when departure is in the future. If a
location replan makes departure immediately due, poll sends the actionable leave
reminder instead of a separate location-update message with the same warning.
Confirmed destination presence still defers travel reminders; leaving makes them
eligible again, with the same leave-wins rule. Failed leave deliveries retain the
existing bounded retry policy.

## Home Assistant alarm

CommuteCompass already pulls live location from Home Assistant when `[home_assistant].enabled = true`. With the optional `[home_assistant.alarm]` block it will *additionally* POST to an HA service every time a `prep` or `leave` ping fires — so HA can wake you up with a real alarm without changing your existing notification channel. The same `HOME_ASSISTANT_TOKEN` is reused; no new env var.

```toml
[home_assistant.alarm]
enabled = true
service = "script.commute_alarm"   # any "domain.service"
kinds   = ["prep", "leave"]        # which ping kinds trigger the alarm

# Optional pass-through merged into the HA service payload
[home_assistant.alarm.extra_data.data.push.sound]
critical = 1
name = "alarm.caf"
```

The recommended pattern is to point `service` at a small HA script you own and chain the loud parts there — iOS does not let third-party apps create real Clock-app alarms programmatically, so something like [Pushcut](https://www.pushcut.io) (its "Notification Server" sustains a custom loud tone until dismissed) plus an HA Companion critical notification fallback is the canonical "wake-from-a-nap" setup. A minimal HA script:

```yaml
script:
  commute_alarm:
    sequence:
      - service: notify.pushcut_my_iphone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
          data:
            sound: "alarm-loop"   # a Pushcut alarm sound
      - service: notify.mobile_app_my_iphone   # belt-and-suspenders critical push
        data:
          title: "{{ title }}"
          message: "{{ message }}"
          data:
            push:
              sound:
                critical: 1
                name: "alarm.caf"
                volume: 1.0
```

CommuteCompass POSTs `{"title": "CommuteCompass", "message": "<ping body>", ...extra_data}` to the configured service. If the call fails, the primary notifier's send is **not** rolled back — the ping is still marked fired and won't repeat next minute.

### Pull-model tomorrow alarm

iOS won't let *any* third party create real Clock-app alarms — only Shortcuts running on-device can. The `commutecompass tomorrow` subcommand bridges that gap without keeping the phone in the loop in real time:

1. CommuteCompass plans tomorrow's events evening-of, picks the earliest `prep_at`, and POSTs it to an HA script.
2. The HA script stores the datetime in an `input_datetime` helper.
3. A daily 21:00 Shortcuts automation on your iPhone (set to "Run Immediately") reads the helper via the HA REST API and creates a Clock-app alarm at that time. No tapping required.

Enable it with:

```toml
[home_assistant.tomorrow]
enabled = true
script  = "script.commute_set_tomorrow_alarm"
```

Drop [`examples/ha/commute_tomorrow_alarm.yaml`](./examples/ha/commute_tomorrow_alarm.yaml) into your HA config (it includes both the `input_datetime` helper and the script, plus the Shortcuts recipe in comments at the bottom). Then schedule `commutecompass tomorrow` to run on an evening systemd timer (e.g. 20:45 NYC); it skips silently when there's nothing on the calendar.

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.default
```

## Wake & nap alarm

Enable `[alarm].enabled = true` and configure `[home_assistant]` plus
`HOME_ASSISTANT_TOKEN`. With the wake window enabled (the default), compare the
healthy calendar leave deadline with `fallback_time` (default noon, NYC), then
subtract `alarm.window.prep_advance_minutes` once from the selected base final.
First ring is that effective final minus `first_alarm_lead_minutes` (default 37).
The shared advance defaults to 0; `calendar_advance_minutes` remains a legacy
alias for the same setting, separate from `prep.prep_minutes`.

For a noon fallback, advance 15, first lead 60 and deadline lead 0, lights begin
at 10:35, first ring is 10:45 and final is 11:45. Advance 0 gives 10:50 / 11:00 /
noon. HA derives lights from the published pair without another offset. Explicit
manual `--leave X` deadlines stay exact and unadvanced. Prep-only and
window-disabled wakes retain `prep_at - wake_lead_minutes` capped by the fallback
clock. Nap caps remain unadvanced: healthy window plans use `leave_at - first
lead`, otherwise `prep_at - wake lead`. Once today's first wake has passed,
publishing targets the next logical day with a future wake; active windows remain
locked. Planning never replaces today's saved plans.

```console
commutecompass alarm publish --dry-run
commutecompass alarm publish
commutecompass alarm status --json
commutecompass alarm nap 30
commutecompass alarm nap --until 15:30
commutecompass alarm skip
commutecompass alarm on     # or off: HA master switch
commutecompass alarm daemon
```

HA owns nap capping/warnings, skip auto-reset, and the final next-alarm
selection. Provision the helpers, scripts and template sensor described in
[`docs/alarm-design.md`](docs/alarm-design.md): `script.commute_publish_alarm`
accepts `wake_at`, `next_prep_at` (ISO strings, or empty prep), sanitized
`next_event`, and `deadline_at` for enabled windows (including fallback) or bound
manual deadlines; `script.commute_nap` accepts `minutes`; `script.commute_alarm_skip`
skips the current next instant. `sensor.commute_next_alarm` exposes the earliest
future unskipped wake/nap instant and `kind`/`event` attributes. Use timezone-aware
ISO sensor values. Persisted UTC text mirrors preserve wake/nap/skip/cap instants
through DST folds and HA restarts; local datetime helpers are display/legacy only.
Missing/unknown UTC helpers retain the last cached schedule; an explicit epoch-zero
value clears it (and means no cap for prep). Provision the full HA layer before use.
HA's `input_button` stop/snooze states must be last-pressed timestamps. The daemon
writes `idle|ringing|snoozed`, a UTC heartbeat and a per-deadline playback acknowledgement.

The desktop daemon caches the last known alarm, rings even if HA goes down,
ignores alarms more than ten minutes stale at startup, and remembers completed
instants across restarts. It uses a case-insensitive PipeWire sink substring
(default `analog`, deliberately not Snapcast Living Room), saves volume/mute,
unmutes, ramps 30→100% over two minutes, loops a synthesized tone (or `sound`
file), and restores audio when playback ends or on SIGTERM. Naps and legacy wakes
have nine-minute snoozes (at most twice), a fifteen-minute timeout and HA stop.
Calendar and fallback windows instead use unlimited capped five-minute snoozes
before final; final has no timeout and ignores HA stop/snooze. Desktop activity
normally dismisses final; master-off and explicit skip/clear remain escape
hatches. See [the window lifecycle](docs/alarm-design.md#window-lifecycle) for
re-ring and recovery details. Configure `sink` so it matches the intended output;
a missing match is an audio error, never a fallback to another room.

For NixOS set `services.commutecompass.alarm.enable = true`, choose the login
`user`/`group` owning PipeWire, and set `alarm.runtimeDir` to its runtime directory
(default `/run/user/1000`). `alarm.publishTime` defaults to `*:0/15`; tomorrow
and poll replans also publish when enabled. The daemon restarts automatically.
The runtime directory/session must exist (use a persistent login/user session).
Activity-stop uses a dedicated `hypridle` monitor (included in the daemon PATH)
and requires a running Wayland session with idle-notify support. It retries
attachment after login or compositor exit; audio still works while unavailable. Set
`alarm.wayland_display` if automatic socket discovery selects the wrong session;
HA stop remains available for naps/legacy wakes and snoozes a window before
final, but cannot dismiss final. `alarm.stop_on_activity` defaults to true.

The iPhone/HA layer is configured separately. The integration is maintained in
the infra repository's `modules/iot/commutecompass-alarm.nix`; the legacy
`examples/ha/commute_tomorrow_alarm.yaml` alone is **not** sufficient for it.

On the iPhone, create a Shortcut to read `sensor.commute_next_alarm`, validate
that the time is a future next occurrence, create and verify a Clock alarm
labelled **CommuteCompass**, then delete older owned alarms and write
`input_datetime.commute_alarm_confirmed`. Run it at 21:00, on charger connection,
and after requesting a nap. Phone alarms only follow skip/off or schedule
changes when this Shortcut runs; HA cannot remotely change Clock.

HA warns about a confirmation mismatch only at 23:30 and sends **one critical
alert** if the desktop daemon is offline or reports playback failure. No repeating
notifications. A healthy audio process cannot detect unplugged physical speakers.
See [`docs/alarm-verification.md`](./docs/alarm-verification.md) for activation
and hardware checks. Keep a manual alarm until those checks pass.
