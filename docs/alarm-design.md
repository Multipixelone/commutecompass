# Wake & nap alarm — design (agreed 2026-10-31, window revised 2026-11-03)

Goal: never need to remember to set a phone alarm. Morning wake and naps are
automatic, commute-aware, and loud.

## Decisions (from grilling session)

The original decisions below describe legacy naps/no-anchor mornings. The
buffered leave-deadline window below overrides commute mornings only.

| # | Decision |
|---|----------|
| Q1 | Layered: real iOS Clock alarm + backup. |
| Q2/Q8 | Legacy morning wake = `min(first_event.prep_at - wake_lead, fallback_time)` on that logical day. No events → `fallback_time` (default `12:00`). |
| Q3 | Naps via HA button and iOS Shortcut (which calls the HA nap script), plus CLI/Telegram `alarm nap`. |
| Q4 | Nap is capped at next `prep_at - wake_lead`; user warned when capped. |
| Q5′ | **Primary = link desktop speakers** (unmute, ramp 30→100 % over 2 min, loop, auto-stop 15 min, restore volume/mute). Secondary = iOS Clock alarm. Fallback = ONE critical alert only if link daemon is unhealthy. No repeating notifications. |
| Q6 | Shortcut hardening: triggers at 21:00 + charger connect + nap shortcut; create and verify a replacement alarm labelled "CommuteCompass", then remove older owned alarms; write back confirmation; HA warns at 23:30 if confirmation ≠ planned wake. |
| Q7 | commutecompass **publishes** times to HA; HA owns nap logic + final "next alarm" selection. |
| Q9 | `commutecompass alarm daemon` long-running on link, polls HA every 30 s, caches last known alarm locally, heartbeats to HA. |
| Q10 | Configurable sink (substring match; default = analog, not snapcast "Living Room"). Legacy stop via HA (button/Shortcut) or keyboard/mouse activity on link (desktop out of reach of bed). Legacy snooze 9 min, max 2. |
| Q11 | link follows the latest plan for pending wakes; a ringing/snoozed wake keeps its active identity across replacement publications (earlier, later, or next day). Phone keeps last confirmed; mismatch warning only at 23:30. |
| Q12 | `input_boolean.commute_alarm_enabled` master switch; "Skip next alarm" auto-resets. CLI `alarm on/off/skip`. |
| Q13 | Configurable sound; default synthesized tone (generated in cache on first start). |
| Q14 | HA lights fade starting 10 min before wake; link audio at wake. Commute timing is L−47 → L−37, defined below. |

Replacement wake publications are not dismissals: explicit clear, master-off,
skip, and desktop activity still end an active wake; legacy stop/timeout still
apply. Commute replans may tighten but never postpone the active window deadline.
Pre-ring rescheduling and nap behavior are unchanged.

Existing iOS Clock and critical-alert behavior is unchanged by this contract
revision. Compatibility with the new **silent notification intent is OPEN for
Finn to decide**; do not silently remove or reinterpret those existing layers.

## Data flow

```
tomorrow job / morning job / replan hook
    → script.commute_publish_alarm(wake_at, next_prep_at, next_event[, deadline_at])
    → HA stores wake/next-prep/event + optional deadline; next prep supplies nap cap
    → sensor.commute_next_alarm = min(wake, nap) respecting enabled/skip
        → link daemon polls → desktop audio ramp + loop
        → HA lights fade (10 min lead-in)
        → iOS Shortcut reads → sets Clock alarm + confirms
        → HA fallback if desktop unhealthy at alarm time → ONE critical alert
```

`alarm.build_payload` always sends `wake_at`, `next_prep_at` (empty if absent)
and `next_event`. It adds `deadline_at` only when the window is enabled (empty
if no anchor); it does not send `prep_at`, `logical_date`, `summary` or
`has_events`. `deadline_at` replaces the former departure publication argument;
it is the buffered door-leave deadline, not a train departure. The coordinated
private infra contract migration is pending; this document does not claim deployment.

## Optional v1 HA contract repair

See [alarm-ha-contract.md](alarm-ha-contract.md) for the opt-in atomic schedule
and advisory session-status contract, including versioned HA declarations for
the infra owner. Legacy defaults remain unchanged. The implementation does not
activate v1, publish upstream, edit infra, or establish live-device behavior.

## State & contracts

### Python additions

- `Config.alarm`: HA entity IDs, wake lead, fallback, sink, sound, ramp, snooze
  and window settings; disabled by default.
- `Plan.depart_at` is retained as informational route-departure metadata only;
  it does **not** anchor the alarm window. `Plan.leave_at` is authoritative.
- CLI: `alarm status | next | nap <minutes> | off | on | skip | daemon`.
- Jobs: publish a wake after planning; republish on timing changes (debounced).
- Daemon: local state cache + small polling loop + PipeWire audio via
  `wpctl`/`pw-play`. No new Python dependencies.

### HA entities (Nix-managed in infra)

- Master: `input_boolean.commute_alarm_enabled`
- Skip: `input_boolean.commute_alarm_skip_next` + skip-until timestamp
- Published `wake_at`, `next_prep_at` (absolute future nap cap), and `next_event`
- Optional `deadline_at` for the commute window
- Nap request timestamp; next-alarm template chooses wake/nap
- Link state: `input_select.commute_alarm_state` (`idle`/`ringing`/`snoozed`)
- Heartbeat: `input_text.commute_alarm_heartbeat_utc` plus compatibility
  `input_datetime.commute_alarm_heartbeat`
- Playback-start acknowledgement: `input_text.commute_alarm_handled_at`, carrying
  the active alarm instant after successful `player.start()` (not a sound proof)
- Audio failure latch: `input_boolean.commute_alarm_audio_failed`
- Shortcut confirmation timestamp + planned wake used
- Buttons/scripts: nap, stop, snooze, skip, on/off
- UTC text deadline helper: `input_text.commute_alarm_deadline_utc`
- UTC text `window_done` helper carrying the **dismissal instant**, treated by HA
  as a completion event; original wake is retained locally in done history for dedupe

The old departure helper is not consumed after upgrade. Rename the Python/HA
publication contract together; leaving the old helper populated is not a
compatibility mechanism. Offset-bearing UTC text is authoritative (including
DST folds); local datetime helpers remain display/compatibility only. Clear a
missing deadline anchor to epoch zero (`1970-01-01T00:00:00+00:00`), never retain
a previous day's deadline.

### Robustness rules

- Cache schedule/state atomically; survive HA downtime and link daemon restart.
- Once a deadline is observed while future, a HA sensor clearing exactly at
  deadline does not cancel it. Enable/skip remain authoritative across outages.
- Never ring an arbitrarily old cached deadline. Every cached state recovered
  after restart expires beyond L+10 min, including previously ringing final.
  Only a continuously live audible final is unbounded.
- A failure anywhere in calendar fetch or planning must not overwrite a good
  wake plan with a no-events fallback. Genuine no-events/unresolved days may use
  fallback; failures keep the prior good publication.
- Wake computation evaluates today + tomorrow so a same-day replan after
  today's wake refreshes tomorrow rather than publishing an elapsed wake.
- Restore original sink volume/mute on every stop/failure path. Playback uses
  the configured local PipeWire sink directly, not Snapcast.
- Local volume/playback timing belongs to a dedicated ~20 Hz monotonic worker,
  independent of HA HTTP polling. Volume commands are serial, skip missed slots
  (no catch-up bursts), and stop joins the worker before restoring the sink;
  no stale worker write may follow restoration. HA polling defaults to 30 s
  idle / **2 s non-idle** (`RINGING_POLL_SECONDS`, including snoozed), with
  bounded HTTP calls; it does not clock the ramp or file loop.
- Volume writes have a 3 s timeout and are best effort: a failed step retries
  at the current monotonic ramp position, never as a catch-up burst. Three
  consecutive failures disable ramp writes after one best-effort 100% attempt;
  playback continues even if that attempt fails. Snapshot/unmute and final
  restoration still report failures. Abnormal player exits/launch failures share
  three recovery retries per ring (0.25/0.5/1 s interruptible backoff); normal
  file looping does not consume that budget or reset the ramp.
- Unrecovered audio failures suppress heartbeat, latch `audio_failed`, and cause HA's
  dead-man fallback to fire once. Heartbeat indicates connectivity, not sound;
  playback acknowledgement is per wake and only follows successful start.
- Cached audio failure is not cleared by process restart or healthy idle HTTP:
  only a later successful playback start clears it. HA mode is reconciled after
  successful reads, but `ringing` is not published while local audio is unhealthy.
  A successful `player.start()` enables the handled-at acknowledgement; pending
  handled-at is discarded if the active alarm changes or audio becomes unhealthy.
  Failed mode, handled-at and failure writes retry on healthy polls while still
  applicable. Heartbeat writes occur only while HA and local audio are healthy,
  at the configured idle poll interval (default 30 s), not every 2 s active poll.
- Cache consumed wake/nap instants, handled stop/snooze timestamps, snooze
  deadline/count, ring start and the explicit enabled latch. Old cache schemas
  load conservatively; absent live enabled state is fail-closed until a true
  enabled value is observed. Shutdown does not falsely publish idle for a
  recoverable ringing/snoozed alarm.
- HA nap cleanup waits until link is not ringing/snoozed. Explicit clearing is
  distinct from the normal future-only sensor transition.
- Existing once-only fallback assessment is in the first three minutes after
  the specific wake: desktop offline OR missing successful playback
  acknowledgement OR `audio_failed`. It is not continuous sound monitoring.

## Buffered leave-deadline wake window (revised 2026-11-03)

### Timeline and anchor

Let **L = `Plan.leave_at`**, the buffered deadline to leave through the door.
The old route-departure D−50 / D−13 design is superseded:

- HA fairy-light fade starts **L−47 min** and reaches full warm at **L−37 min**.
- First desktop alarm is **L−37 min**.
- Final alarm is **L**, with `deadline_lead_minutes = 0`.
- `first_alarm_lead_minutes = 37`; configuration input is
  `alarm.window.deadline_utc_entity = "input_text.commute_alarm_deadline_utc"`.
- Safety, weather and realtime buffers are already subtracted by the planner
  when forming `leave_at`, so they shift the **whole window earlier**. Do not
  subtract them again or substitute informational `Plan.depart_at`.

Source check: `planner.py` computes
`buffer_minutes = safety_buffer_minutes + wx.minutes + rt.minutes`, then
`leave_at = departure - buffer` using elapsed timestamp arithmetic. For a
non-approximate route it uses validated route timing; otherwise it derives
`departure` from event start minus duration. `prep_at = leave_at - prep_minutes`.
The directions fixture's initial WALKING step and route departure are epoch
`1746864000`; the later transit boarding is `1746864300`. Thus the route
anchor includes walking out the door, not boarding the train. See the concrete
verification expectations in [alarm-verification.md](alarm-verification.md).

A commute window is opt-in and requires a published wake that matches
`deadline_at - first_alarm_lead_minutes` exactly. No matching deadline (including
fallback/no-anchor mornings) means the **legacy path byte-for-byte**: configured
first ramp, nine-minute snooze, maximum two snoozes, fifteen-minute terminal
timeout, and HA stop works. Naps always retain those legacy semantics.

### Window lifecycle

1. Before first ring, follow replans normally. Lock the deadline **only at first
   ring**, not at publication or daemon observation. Persist original wake key,
   deadline and phase. Once locked, only an earlier deadline may tighten the
   window; a later replan cannot extend it. Master-off, explicit skip/clear, or
   desktop activity ends the session.
2. First ring uses the configured first-alarm ramp. Before L, manual snooze and
   HA stop both snooze for `min(now + 5 min, L)` with **unlimited** repeats.
   A fifteen-minute ring timeout is now an **automatic five-minute snooze**,
   likewise capped at L, not a terminal dismissal.
3. Every pre-final re-ring after snooze starts at **20% absolute sink volume**,
   holds for **30 s**, then ramps over **60 s** to **100%**, repeatedly as needed.
   This is not 20% of the saved desktop volume. Preserve restoration ownership.
4. At L, a snoozed window wakes into final with the configured **first-alarm
   ramp**, not the re-ring curve. Final has **no snooze, no HA stop, and no
   timeout**: desktop keyboard/mouse activity is the normal dismissal, with
   master-off or explicit skip/clear as escape hatches. If audio is already
   ringing at L, transition to final **without restarting audio or its ramp**.
5. Desktop dismissal of an active window, including during snooze, consumes the
   original wake and remembers it in local done history for dedupe.
   `PublishWindowDone` sends the completion timestamp **now**, not the original
   wake key: `window_done` carries the offset-bearing UTC dismissal instant.
   HA treats the helper update as a completion event to restore lights and
   clear its notification. Preserve the pending completion timestamp across
   failed writes, offline operation and restart.

On restart, recover **any cached window state** only through **L+10 min
inclusive**, including a previously final-ringing state; beyond that boundary,
skip/consume it and log the expired recovery. **Only a continuously live audible
final is unbounded** until dismissal. A restarted final is not exempt from the
recovery cutoff. Warn visibly if activity monitoring is disabled or unavailable:
final then cannot be dismissed by desktop activity and needs master-off or
explicit skip/clear. A connected HA is not a substitute for an activity monitor.

## Private infra follow-up contract — pending, NO deployment

These are the required coordinated changes in the separate private infra repo,
not evidence of evaluated or deployed configuration here:

- `script.commute_publish_alarm` accepts **`deadline_at`**, writes offset-bearing
  UTC text to `input_text.commute_alarm_deadline_utc`, and clears a missing anchor
  to epoch zero. Replace the old departure argument/helper consumer; do not
  fall back to it. `window_done` carries the offset-bearing UTC dismissal
  instant from `PublishWindowDone`; HA treats its update as a completion event.
  The original wake is remembered locally in done history for dedupe, not sent
  as the helper value. HA must retain the current light/notification session's
  wake instant and accept completion only when its timestamp is at or after
  that wake (and not in the future). A delayed retry for yesterday must not
  restore lights or clear the notification for today's newer session.
- HA fades `light.fairy_lights` from **L−47 to full warm at L−37**. Save
  `switch.fairy_lights_sync_receive` and `select.fairy_lights_live_override`,
  disable sync for the window, and restore those saved states on the
  `window_done` completion event. First ring turns on `light.smart_led_bulb`.
- Every transition into **ringing** sends a **silent** actionable notification
  through `notify.mobile_app_nougat`. Include **Snooze**, pressing
  `input_button.commute_alarm_snooze`. Include **Took meds** only while
  `input_boolean.morning_meds_taken` is off; its action toggles it once, never
  toggles it back on repeated/stale taps. Clear the notification on the
  `window_done` completion event. Notification actions do not bypass final-phase rules.
- HA stop maps to the same five-minute capped snooze **before final** and is
  **ignored in final**. Do not wire it to unconditional final dismissal.
- Leave existing iOS Clock and critical-alert behavior untouched. Whether those
  layers are compatible with the silent intent remains **OPEN: Finn decision**.

No infra edits, activation or deployment are part of this documentation change.
The revised contract passed full pytest, global Ruff and strict mypy validation;
see [alarm-verification.md](alarm-verification.md) for evidence. Coordinated infra
migration and live verification remain pending.
