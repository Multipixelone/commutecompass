# Wake & nap alarm — design (agreed 2026-10-31)

Goal: never need to remember to set a phone alarm. Morning wake and naps are
automatic, commute-aware, and loud.

## Decisions (from grilling session)

| # | Decision |
|---|----------|
| Q1 | Layered: real iOS Clock alarm + backup. |
| Q2/Q8 | Morning wake = `min(first_event.prep_at - wake_lead, fallback_time)` on that logical day. No events → `fallback_time` (default `12:00`). |
| Q3 | Naps via HA button and iOS Shortcut (which calls the HA nap script), plus CLI/Telegram `alarm nap`. |
| Q4 | Nap is capped at next `prep_at - wake_lead`; user warned when capped. |
| Q5′ | **Primary = link desktop speakers** (unmute, ramp 30→100 % over 2 min, loop, auto-stop 15 min, restore volume/mute). Secondary = iOS Clock alarm. Fallback = ONE critical alert only if link daemon is unhealthy. No repeating notifications. |
| Q6 | Shortcut hardening: triggers at 21:00 + charger connect + nap shortcut; create and verify a replacement alarm labelled "CommuteCompass", then remove older owned alarms; write back confirmation; HA warns at 23:30 if confirmation ≠ planned wake. |
| Q7 | commutecompass **publishes** times to HA; HA owns nap logic + final "next alarm" selection. |
| Q9 | `commutecompass alarm daemon` long-running on link, polls HA every 30 s, caches last known alarm locally, heartbeats to HA. |
| Q10 | Configurable sink (substring match; default = analog, not snapcast "Living Room"). Stop via HA (button/Shortcut) or any keyboard/mouse activity on link (the desktop is out of reach of bed). Snooze via HA. Snooze 9 min, max 2. |
| Q11 | link follows latest plan always; phone keeps last confirmed; mismatch warning only at 23:30. |
| Q12 | `input_boolean.commute_alarm_enabled` master switch; "Skip next alarm" auto-resets. CLI `alarm on/off/skip`. |
| Q13 | Configurable sound; default synthesized tone (generated in code, no licensing). |

## Architecture

```
commutecompass alarm publish (timer + tomorrow job + poll replans)
   └─POST script.commute_publish_alarm {wake_at, next_prep_at, next_event}
HA
   input_datetime.commute_wake_at        ← publish
   input_datetime.commute_next_prep_at   ← publish   (effective nap/snooze cap)
   input_text.commute_next_event         ← publish
   input_datetime.commute_nap_at         ← script.commute_nap
   input_datetime.commute_alarm_skipped_at ← script.commute_alarm_skip
   input_boolean.commute_alarm_enabled   (master)
   sensor.commute_next_alarm (template)  = earliest FUTURE of {wake_at, nap_at},
                                           excluding == skipped_at; "unknown" if disabled.
                                           attributes: kind (wake|nap), event
   input_button.commute_alarm_stop / commute_alarm_snooze / commute_nap_30
   input_select.commute_alarm_state      (idle|ringing|snoozed) ← daemon
   input_datetime.commute_alarm_heartbeat ← daemon every poll
   input_datetime.commute_alarm_confirmed ← iOS Shortcut
   input_text.commute_wake_utc / commute_nap_utc / commute_alarm_skipped_utc
   input_text.commute_next_prep_utc ← authoritative offset-bearing instant mirrors
   input_text.commute_alarm_heartbeat_utc ← authoritative UTC heartbeat
   input_text.commute_alarm_handled_at ← daemon successful playback acknowledgement
   input_boolean.commute_alarm_audio_failed ← daemon playback-failure latch
   input_text.commute_alarm_fallback_claims ← HA-owned once-only ledger
   automations: nap button → script; offline/audio-failed critical fallback
                (once in deadline+1min to +3min window);
                23:30 confirmation mismatch warning; retain elapsed nap 10 min.
link: commutecompass alarm daemon (systemd, user tunnel, XDG_RUNTIME_DIR for PipeWire)
   polls sensor.commute_next_alarm + buttons, rings via wpctl/pw-play.
```

### Wake computation (`commutecompass.alarm.compute_wake`)
- Target logical day: today's wake if still in the future, else tomorrow's.
- Plans for the day: earliest non-None `prep_at` across planned events (same
  planning as `jobs/tomorrow.py`; no DB upsert).
- `wake = min(prep_at - wake_lead, fallback_time on that day)`; no plans → fallback.
- `next_prep_at` = earliest future `prep_at` from today's/tomorrow's plans,
  minus `wake_lead` — the effective hard cap for HA naps and daemon snoozes.
  The cap itself can already have passed, in which case a nap is refused. May be null.
- HA retains a just-due wake for the three-minute fallback window before ordinary
  next-day rollover; active desktop playback/snooze retains its original bounds.
- UTC text mirrors, not the local display helpers, preserve the intended instant
  through a fall-back DST fold and HA restart. Existing datetime helpers remain
  for display and legacy compatibility. Missing/unknown UTC authority retains
  cached scheduling; explicit epoch zero clears a schedule or removes the prep
  cap. The daemon never reconstructs authority from local display values.

### Config `[alarm]` (`config.AlarmConfig`)
```toml
[alarm]
enabled = false
fallback_time = "12:00"          # HH:MM NYC
wake_lead_minutes = 0
publish_script = "script.commute_publish_alarm"
nap_script = "script.commute_nap"
skip_script = "script.commute_alarm_skip"
next_alarm_entity = "sensor.commute_next_alarm"
enabled_entity = "input_boolean.commute_alarm_enabled"
stop_button = "input_button.commute_alarm_stop"
snooze_button = "input_button.commute_alarm_snooze"
state_entity = "input_select.commute_alarm_state"
heartbeat_entity = "input_datetime.commute_alarm_heartbeat"
wake_entity = "input_datetime.commute_wake_at"
nap_entity = "input_datetime.commute_nap_at"
skipped_entity = "input_datetime.commute_alarm_skipped_at"
next_prep_entity = "input_datetime.commute_next_prep_at"
wake_utc_entity = "input_text.commute_wake_utc"
nap_utc_entity = "input_text.commute_nap_utc"
skipped_utc_entity = "input_text.commute_alarm_skipped_utc"
next_prep_utc_entity = "input_text.commute_next_prep_utc"
handled_entity = "input_text.commute_alarm_handled_at"
heartbeat_utc_entity = "input_text.commute_alarm_heartbeat_utc"
audio_failed_entity = "input_boolean.commute_alarm_audio_failed"
stop_on_activity = true
wayland_display = ""
sink = "analog"                  # case-insensitive substring of PipeWire sink description/name
sound = ""                       # path; empty = synthesized tone
ramp_start_percent = 30
ramp_seconds = 120
max_ring_minutes = 15
snooze_minutes = 9
max_snoozes = 2
poll_seconds = 30
cache_path = ""                  # empty = <db dir>/alarm-cache.json
```

### CLI (`commutecompass alarm …`)
`publish [--dry-run]`, `status [--json]`, `nap MINUTES | nap --until HH:MM`,
`skip`, `on`, `off`, `daemon`. Exit codes per cli.py conventions
(HA unreachable → EXIT_TRANSIENT, disabled/misconfig → EXIT_CONFIG).

### Daemon state machine
idle → (now ≥ next_alarm, not already rung for that instant) ringing →
stop button pressed after ring start → idle (mark instant done) |
snooze pressed & snoozes < max → snoozed (re-ring at +snooze_minutes) |
max_ring_minutes elapsed → idle. Poll 2 s while ringing.
HA unreachable → use cached next_alarm (local JSON). Never ring an alarm
instant more than 10 min stale on startup (missed alarm > late alarm).
Audio: resolve sink via `wpctl status`/`pw-dump`; save volume+mute; unmute,
set volume ramp; loop `pw-play --target <node>`; restore on stop/exit (also on
SIGTERM).
