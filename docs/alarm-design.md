# Wake & nap alarm — design (agreed 2026-10-31, window revised 2026-11-07)

Goal: never need to remember to set a phone alarm. Morning wake and naps are
automatic, commute-aware, and loud.

The shared calendar/fallback advance below is the agreed 2026-11-07 contract,
reconciled against source and verified by offline tests, lint, type checks and
Nix checks/build; see [alarm-verification.md](alarm-verification.md).
This is not a deployment or live-device claim.

## Decisions (from grilling session)

The original legacy wake rule below applies when the window is disabled. The
wake-window contract overrides both healthy calendar windows and fallback
mornings when enabled; naps and prep-only wakes retain their legacy behavior.

| # | Decision |
|---|----------|
| Q1 | Layered: real iOS Clock alarm + backup. |
| Q2/Q8 | Legacy morning wake = `min(first_event.prep_at - wake_lead, fallback_time)` on that logical day. No events → `fallback_time` (default `12:00`). |
| Q3 | Naps via HA button and iOS Shortcut (which calls the HA nap script), plus CLI/Telegram `alarm nap`. |
| Q4 | Nap is capped at published `next_prep_at`; the unadvanced calendar-window / legacy-prep calculations are defined below. User warned when capped. |
| Q5′ | **Primary = link desktop speakers** (unmute, ramp 30→100 % over 2 min, loop, auto-stop 15 min, restore volume/mute). Secondary = iOS Clock alarm. Fallback = ONE critical alert only if link daemon is unhealthy. No repeating notifications. |
| Q6 | Shortcut hardening: triggers at 21:00 + charger connect + nap shortcut; create and verify a replacement alarm labelled "CommuteCompass", then remove older owned alarms; write back confirmation; HA warns at 23:30 if confirmation ≠ planned wake. |
| Q7 | commutecompass **publishes** times to HA; HA owns nap logic + final "next alarm" selection. |
| Q9 | `commutecompass alarm daemon` long-running on link, polls HA every 30 s, caches last known alarm locally, heartbeats to HA. |
| Q10 | Configurable sink (substring match; default = analog, not snapcast "Living Room"). Legacy stop via HA (button/Shortcut) or keyboard/mouse activity on link (desktop out of reach of bed). Legacy snooze 9 min, max 2. |
| Q11 | link follows the latest plan for pending wakes; a ringing/snoozed wake keeps its active identity across replacement publications (earlier, later, or next day). Phone keeps last confirmed; mismatch warning only at 23:30. |
| Q12 | `input_boolean.commute_alarm_enabled` master switch; "Skip next alarm" auto-resets. CLI `alarm on/off/skip`. |
| Q13 | Configurable sound; default synthesized tone (generated in cache on first start). |
| Q14 | HA lights fade starting 10 min before the published first wake; link audio at wake. Calendar and fallback windows use the same geometry below. |

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

Legacy `alarm.build_payload` always sends `wake_at`, `next_prep_at` (empty if
absent) and `next_event`. It adds `deadline_at` when the window is enabled or a
manual deadline is bound; an empty deadline means this wake has no window.
Enabled fallback windows carry a real deadline, not an empty anchor. The payload
does not send `prep_at`, `logical_date`, `summary` or `has_events`.
`deadline_at` replaces the former departure argument: it is the effective alarm
final derived from the buffered door-leave time or fallback base, not a train
departure. This shared-advance revision changes timestamp selection, not the
legacy/v1 wire schemas; deployment is separate.

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
- CLI: `alarm publish | status | wake | nap | off | on | skip | daemon`.
- Jobs: publish a wake after planning; republish on timing changes (debounced).
- Daemon: local state cache + small polling loop + PipeWire audio via
  `wpctl`/`pw-play`. No new Python dependencies.

### HA entities (Nix-managed in infra)

- Master: `input_boolean.commute_alarm_enabled`
- Skip: `input_boolean.commute_alarm_skip_next` + skip-until timestamp
- Published `wake_at`, `next_prep_at` (absolute future nap cap), and `next_event`
- Optional `deadline_at` for calendar, fallback or manual wake windows
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
- Calendar/planning failure is not a successful empty day. Keep a valid retained
  future publication when available; otherwise preserve the existing usable
  manual / next-logical-day safety fallback path and explicit warning. With the
  window enabled, a newly computed safety fallback uses the same advanced
  fallback geometry; retained effective timestamps are not advanced twice.
- Wake computation evaluates today + tomorrow so a same-day replan after
  today's first wake refreshes tomorrow rather than inventing another morning
  wake. The bounded third-logical-day search and active daemon lock remain
  unchanged; a replacement publication does not dismiss today's active window.
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

## Calendar and fallback wake window (revised 2026-11-07)

### Timeline and anchor

With `alarm.window.enabled = true`, use one geometry for calendar and fallback
windows:

- **B (base final deadline)** is a healthy plan's buffered `Plan.leave_at`, or
  `alarm.fallback_time` on the target NYC logical day. Fallback time is **not**
  the first ring when the window is enabled.
- **A = `alarm.window.prep_advance_minutes`** is the one shared advance, default 0.
- **L = B − A** is the effective final / published `deadline_at`, with
  `deadline_lead_minutes = 0`.
- **W = L − `first_alarm_lead_minutes`** is the published `wake_at` / first ring.
- HA lights begin **W−10 min** and reach full warm at **W**. They consume these
  published timestamps, not an independently adjusted base time.

The default first lead remains 37 minutes: lights L−47, first L−37, final L.
A 60-minute first lead instead gives lights L−70, first L−60, final L. There is
**no second 15-minute buffer**. The old route-departure D−50 / D−13 design is
superseded; do not anchor to informational `Plan.depart_at`.

Calendar and fallback candidates compare their **effective finals** on the same
logical day, choosing the earlier window. Do not compare an advanced calendar
first ring to an unadvanced fallback clock: that can select a later final.
Prep-only wakes without a healthy leave anchor retain `prep_at - wake_lead_minutes`
and do not receive this advance. Window-disabled automatic waking retains the
legacy rule `min(first prep - wake lead, fallback_time)` with no prep advance.
`alarm.enabled` remains false by default; the window setting itself defaults true.

Source check: `planner.py` computes
`buffer_minutes = safety_buffer_minutes + wx.minutes + rt.minutes`, then
`leave_at = departure - buffer` using elapsed timestamp arithmetic. For a
non-approximate route it uses validated route timing; otherwise it derives
`departure` from event start minus duration. `prep_at = leave_at - prep_minutes`.
Those travel buffers already shift the whole calendar window earlier; do not
subtract them again. `prep.prep_minutes` is a separate route/event preparation
lead, **not** the alarm's shared prep advance. Neither route times, prep times
nor commute pings are changed by alarm tuning.

The directions fixture's initial WALKING step and route departure are epoch
`1746864000`; the later transit boarding is `1746864300`. The route anchor includes
walking out the door, not boarding the train. See
[alarm-verification.md](alarm-verification.md) for evidence and acceptance cases.

### Shared prep advance and compatibility

`alarm.window.prep_advance_minutes` is an integer from 0–1440, default 0, used
once for both healthy calendar and fallback windows. It allows morning-task time
without changing the width of the first-to-final window. The old
`alarm.window.calendar_advance_minutes` spelling remains an input alias for
**the same value**, not a calendar-only control or an additional buffer. If both
keys are present their values must agree; conflicting values are rejected on
load and by set/unset without rewriting the file. The configuration owner must
repair a conflict in the source before using those commands. Effective config
output uses the canonical `prep_advance_minutes` name.

On an authorized writable config, `commutecompass config set
alarm.window.prep_advance_minutes 15` sets the shared value. `config set` with the
legacy name addresses the same setting and writes the canonical key; `config
unset` with either name removes both spellings so the zero default applies.
`config reset` also clears that single setting. For Nix-managed configuration,
edit the declarative source through its owner rather than the generated `/etc`
file. See the [allowlist reference](../skills/commutecompass/references/config-allowlist.md).

Manual `alarm wake --leave X` is exact: no prep advance is subtracted from X.
With deadline lead zero, its first ring is X minus the first lead and its final
is X. Manual/calendar conflict selection compares X to the calendar's effective
final; an earlier calendar deadline wins unless `--force` selects manual. Fallback
does not displace manual intent. Retained schedules already contain effective
timestamps and are not advanced again.

The nap cap (`next_prep_at`) retains its unadvanced calculation: window-enabled
calendar plans contribute `leave_at - first_alarm_lead_minutes`; prep-only or
window-disabled plans contribute `prep_at - wake_lead_minutes`. It can differ
from the published first ring. A fallback window does not invent a nap cap when
there is no eligible plan. Nap duration, snoozes and ring profiles remain legacy.

All lead/advance subtraction uses UTC elapsed minutes, including across DST.
Logical days still start at 02:00 in `America/New_York`; event membership follows
the event's start, not a shifted wake. A fallback clock before 02:00 belongs to
the following calendar date of that logical day. Subtracting the advance/lead
must not reassign that day if the first ring crosses midnight or the day boundary.
Master-off, skip, manual conflict/force, and active-window locking are unchanged.

### Worked times (60-minute first lead, zero deadline lead)

For fallback base `alarm.fallback_time = "12:00"`, without an earlier calendar
window, the expected NYC times on an ordinary day are:

| Output | Advance 0 | Advance 15 |
|---|---|---|
| Fallback base final | 12:00 | 12:00 |
| Lights begin | 10:50 | 10:35 |
| First ring / published wake | 11:00 | 10:45 |
| Final / published deadline | 12:00 | 11:45 |

For a healthy calendar plan with buffered leave 09:00 and route departure 09:13:

| Output | Advance 0 | Advance 15 |
|---|---|---|
| Lights begin | 07:50 | 07:35 |
| First ring | 08:00 | 07:45 |
| Final / published deadline | 09:00 | 08:45 |
| Nap cap | 08:00 | 08:00 |
| Buffered leave / route departure | 09:00 / 09:13 | 09:00 / 09:13 |

Manual `--leave 09:00` remains first 08:00 / final 09:00 with either advance.

### Infra integration guidance — separate work, not executed

After source reconciliation and validation, the infra owner must separately roll
the application revision and review its declarative configuration. Supplied
read-only inspection identifies `infra/modules/link/commutecompass.nix:54–70`
as the overlay owner for `services.commutecompass.configFile`: it sets
`alarm.enabled = true`, the legacy publication contract, and the first lead from
`infra/lib/commute-alarm.nix` (60 minutes). That is source evidence, not evidence
of activation. No public advance setting was found there. Do not inspect private
TOML to populate public examples.

For the agreed 15-minute advance / 60-minute window, merge these settings into
the owner's configuration (not a replacement for the rest of the file):

```toml
[alarm]
fallback_time = "12:00"  # optional choice of BASE final; not alarm.window.fallback_time

[alarm.window]
enabled = true
prep_advance_minutes = 15
first_alarm_lead_minutes = 60
deadline_lead_minutes = 0
```

If the private source already sets `calendar_advance_minutes`, its owner must
remove/migrate it or keep it equal to 15 before adding the canonical key; do not
leave conflicting aliases in the merged TOML. No new Nix module option or
downstream package patch is required. Config changes do not roll the application
revision or prove that a new schedule has been published. Activation, restart,
republishing and playback need separate authorization and verification.

Supplied read-only inspection of
`infra/modules/iot/commutecompass-wake-window.nix:123–128,459` found the existing
consumer requires `deadline - wake = 60 minutes`, begins pending at `wake - 10
minutes`, and bases the fade transition on the published wake/deadline. **No light
logic change or extra advance is needed for that consumer.** Do not derive lights
from `next_prep_at`, and never subtract A again. Keep any v1 validation's first
lead consistent with the producer (3600 seconds for this example). Both legacy
and v1 carry the same effective wake/deadline geometry; no wire schema changes.

`deadline_lead_minutes = 15` is not a substitute for the shared advance: it moves
only final ringing, shortens the window, and also moves manual final. Keep it at
zero for the geometry above and exact manual `--leave` semantics.

### Window eligibility

A window requires an enabled daemon window and a published wake matching
`deadline_at - first_alarm_lead_minutes` exactly. Enabled fallback publications
now supply that pair and use the **same window lifecycle** as calendar wakes.
They are no longer noon-only legacy alarms. Naps, prep-only wakes, and
window-disabled automatic wakes retain the configured legacy first ramp,
nine-minute snooze, maximum two snoozes, fifteen-minute terminal timeout, and
working HA stop. A missing/mismatched legacy deadline cannot open a window;
invalid v1 tuples remain no new information rather than an automatic legacy
fallback. Final dismissal rules below apply equally to calendar and fallback
windows.

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

These are consumer requirements for the separate infra owner to verify or
migrate where not yet satisfied, not evidence of evaluated or deployed
configuration. The supplied inspection above already confirms the 60-minute
light geometry; this revision does not require changing that light logic:

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
- HA fades `light.fairy_lights` from **published wake−10 minutes to full warm at
  the published wake** (L−47 to L−37 at the default first lead, L−70 to L−60 at
  a 60-minute lead). Do not subtract the prep advance again. Save
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

No infra edits, activation or deployment are part of this change.
[alarm-verification.md](alarm-verification.md) separates historical validation
from the passing shared-advance/fallback revision checks. Read-only infra
inspection establishes the existing 60-minute light geometry, not deployment
of this application revision. Live verification remains separately authorized
owner work.
