# Alarm verification and activation

## Claims and evidence owners

- Publisher lane: commute wake is buffered `Plan.leave_at` (L) minus 37 minutes;
  final is L. No-anchor mornings retain the prep-minus-lead/noon fallback and
  naps retain the future prep-minus-lead cap, with NYC/DST correctness.
  Replanning never replaces tomorrow with an elapsed wake. Calendar/planning
  failure never masquerades as safe no-events fallback. Evidence required:
  pure computation and mocked jobs tests.
- Daemon lane: future-only HA sensor transitions do not swallow deadlines;
  skip/off/explicit clear are authoritative even across cached outages;
  phase-dependent stop/snooze/timeouts and restart state work; audio failure
  is visible to fallback. Evidence required: fake HA snapshots, fake clock,
  fake audio/subprocess tests and cache reload scenarios.
- HA lane: helpers, scripts, templates and notifications share the Python
  contract. Existing fallback sends once if desktop is offline or playback
  fails, not a periodic notification storm. Evidence required: evaluated Nix
  host config, rendered YAML and Jinja scenarios. These do not establish
  live-device delivery. The coordinated private infra migration is **pending**.
- Orchestrator after writers reconcile: run assigned pytest/ruff/mypy checks
  in a delegated validation lane; evaluate package/module integration
  separately. Report baseline failures rather than hiding them. This docs-only
  lane records source documentation; the reconciled validation run is reported
  in the revised wake-window evidence section below.

## Changes to earlier design notes

- The route-departure D−50 / D−13 design is superseded by **L−37 / L**, where
  **L = buffered `Plan.leave_at`**. Safety/weather/realtime buffers shift lights,
  first ring and final earlier together. `Plan.depart_at` is informational only.
- `alarm.build_payload` sends **`wake_at`, `next_prep_at`, `next_event`**;
  `next_prep_at` is empty if absent. Only window-enabled payloads add
  **`deadline_at`** (empty if no anchor). No `prep_at`, `logical_date`, `summary`
  or `has_events` arguments are sent.
- Publication uses **`deadline_at`** and configuration reads
  **`input_text.commute_alarm_deadline_utc`**, replacing the departure helper.
  Defaults are `first_alarm_lead_minutes = 37` and
  `deadline_lead_minutes = 0`. The upgraded consumer does not read the old
  helper. Missing anchors must be cleared to offset-bearing epoch-zero text,
  `1970-01-01T00:00:00+00:00`, not left stale.
- Keyboard/mouse activity is enabled: the desktop is out of reach of bed.
  Before final, HA stop means five-minute capped snooze; final ignores HA stop
  and snooze. Master-off and explicit skip/clear remain escape hatches.
- Heartbeat proves connectivity, not functioning sound. Existing fallback also
  requires acknowledgement of the specific wake after successful playback
  start; `input_boolean.commute_alarm_audio_failed` overrides it on playback
  failure. This is one assessment in the first three minutes, not continuous
  delivery monitoring. Audio failure remains latched across restart and healthy
  idle polling until a successful later playback start.
- Offset-bearing UTC text is authoritative for wake, deadline, nap, skip, cap,
  heartbeat and playback acknowledgement. Local `input_datetime` helpers are
  display/legacy values and cannot alone preserve a fall-back fold instant
  across HA restart.
- Ordinary rollover retains a just-due wake through the fallback window and
  does not cancel active ringing/snooze. After restart, **all cached window
  states**, including previously final-ringing states, recover only through
  **L+10 min inclusive** and expire beyond it. Only a **continuously live audible
  final** is unbounded; restarting it does not bypass the recovery cutoff.
- Local audio's ~20 Hz monotonic worker owns ramps and looping independently
  of HA polling: default 30 s idle, **2 s non-idle** (`RINGING_POLL_SECONDS`,
  including snoozed). Commands stay serial with no catch-up burst; stop joins
  the worker before restoring the original sink mute/volume. Cached failure
  state survives restart; pending completion delivery follows the contract above.
- HA mode is `input_select.commute_alarm_state` (`idle`/`ringing`/`snoozed`),
  reconciled after successful reads; unhealthy audio suppresses a `ringing`
  publication. Successful `player.start()` enables
  `input_text.commute_alarm_handled_at` with the active alarm instant; it is
  playback-start acknowledgement, not proof of audible output. A pending
  acknowledgement is discarded if the active alarm changes or audio becomes
  unhealthy. Mode/handled-at/failure writes retry while applicable on healthy
  polls. Heartbeat updates `input_text.commute_alarm_heartbeat_utc` plus local
  compatibility `input_datetime.commute_alarm_heartbeat` only while HA/audio
  are healthy, at the configured idle poll interval (default 30 s), not every
  2 s active poll.
- The phone Shortcut creates and verifies a replacement before deleting the
  old owned alarm. iOS Clock alarms cannot be edited remotely by HA: phone
  skip/off/replans apply only when the Shortcut runs. Confirmation warns about
  divergence; it does not prove the phone will make sound.
- Leave iOS Clock and existing critical-alert behavior untouched. Compatibility
  with the new silent notification intent is **OPEN: Finn decision**.

## Historical source-validation result (2026-11-01; not revised-window evidence)

The following records the earlier validation only. It does **not** establish
that the new leave-deadline contract passes:

- Full pytest: **944 passed, 10 failed**. The same ten poll-replanning test
  failures reproduced on unchanged `192a310`; they were baseline failures, not
  a clean suite. Fixtures used today's date on the DST-transition day, but the
  definitive cause was not established. Ruff and strict mypy passed.
- Actual link module evaluation with a local package override and temporary
  alarm-enabled config passed; targeted package, daemon/publish units and timer
  built. This proved wiring, not activation or audible sound.
- Earlier reconciled checks: **262 targeted tests passed**, global Ruff passed
  and strict mypy passed (31 source files). The subsequent daemon-only boundary
  correction passed **78 daemon tests** and repeated global Ruff/mypy; unchanged
  areas retained earlier integrated evidence. Publisher lane: 57 tests passed.
  Coverage included historical-day publication, due-wake rollover, interrupted
  playback, bounded snooze recovery, UTC folds, playback acknowledgement and
  activity reattachment.
- Rendered HA YAML parsing, 130 Jinja syntax checks and focused scenarios
  passed, including DST-fold heartbeat, deadline retention, cap, skip,
  acknowledgement and cleanup. No live HA change or notification was made.
- That package/module build predated the final lifecycle/UTC corrections; it
  established unchanged unit/environment wiring, not a final host build.
- Earlier bounded static safety gate passed for lifecycle restart/cleanup,
  no-cap sentinel and unknown UTC authority. This was source approval, not
  device activation evidence.

## Revised wake-window evidence (2026-11-03): validated

Reconciled L−37 / L revision, with no live HA writes or audio:

- Full pytest through `agent-run-long` (`leave-window-final-pytest`, 20-minute
  bound, `nix develop -c pytest -q`): **1,443 passed**.
- Global Ruff and strict mypy through `agent-run-long`
  (`leave-window-final-lint`, 10-minute bound): both passed; mypy checked
  **34 source files**.
- After test-only typing fixes, `leave-window-typefix-pytest` repeated the full
  suite: **1,443 passed in 47.04s**. `leave-window-typefix-lint` passed global
  Ruff and `mypy src tests/test_alarm_window.py` (**35 files**, including the
  additional test module). Neither test typing fix uses ignores.
- `git diff --check` passed. Independent state-machine review found no
  remaining blocking issues after the rollover recovery correction.
- No revised infra/module evaluation, deployment, live delivery or audible
  verification is claimed. Those remain follow-up activation gates below.

The full suite includes the following cases in `tests/test_alarm.py`,
`tests/test_alarm_window.py`, `tests/test_alarm_ramp.py` and planner/routing tests.

### Door-leave anchor: source checked and test suite passed

- `tests/fixtures/directions_sample.json` starts with a WALKING step whose
  `departure_time.value` is **1746864000**; the route-level departure has the
  same epoch. Boarding the transit leg is **1746864300**, five minutes later.
  `tests/test_routing.py::test_nested_boarding_time_drives_realtime` asserts
  route departure at 1746864000 and transit boarding at 1746864300. This is
  evidence of door-walk versus train-boarding identity; the regression passed
  as part of the full suite above.
- Source inspection of `src/commutecompass/planner.py` confirms it sums
  `config.prep.safety_buffer_minutes + wx.minutes + rt.minutes`, then subtracts
  that buffer from route departure using timestamp arithmetic to form
  `leave_at`. `prep_at` subtracts preparation time from `leave_at`. For
  approximate/no-exact-time routes it derives departure from event start minus
  duration. `Plan.depart_at` retains that unbuffered departure for information.
- Verify with the fixture that L equals **1746864000 minus all buffers**,
  `deadline_at` publishes L, first ring is L−37, and lights start L−47.
  Increasing safety/weather/realtime buffer must move the complete window
  earlier by exactly that increment, without double subtraction. Do not anchor
  to the transit boarding time, event start, or informational `Plan.depart_at`.

### Publisher, lifecycle and regression checklist

- Publication/config migration: `deadline_at`, deadline UTC entity, 37/0
  defaults, UTC folds and epoch-zero clear; old departure helper not consumed.
  Only exact matching wake/deadline pairs enable the window. A genuine
  no-anchor day clears yesterday's anchor; planning failure retains good state.
- First ring uses the configured first ramp. Deadline locks **at first ring**,
  not at observation/publication; after lock earlier replans tighten it and
  later replans cannot extend it. Persist the original wake key.
- Before final, manual snooze and HA stop are unlimited `min(now+5 min, L)`.
  Fifteen-minute timeout auto-snoozes the same way instead of consuming the
  window. Repeat re-rings use **20% absolute sink**, hold **30 s**, ramp **60 s**
  to **100%**; not a fraction of the saved pre-alarm volume.
- At L, a snoozed window starts the configured first ramp; an already-ringing
  window transitions without restarting audio/ramp. Final ignores snooze and
  HA stop and has no timeout. Desktop activity dismisses; master-off or explicit
  skip/clear also ends it. Activity during snooze consumes the same session.
- Restart recovery of **every cached window state** includes exactly L+10 min
  and excludes later instants with skip/log, including cached final ringing.
  Only a continuously live audible final is unbounded. Cover cached HA outage,
  old cache schemas, restart mid-snooze or final, DST folds and enabled latch.
  Warn if activity monitoring is disabled/unavailable.
- Successful desktop dismissal remembers the original wake in local done
  history for dedupe, even after earlier replan. `PublishWindowDone` sends
  completion timestamp **now**, not the original wake key; `window_done` carries
  the offset-bearing UTC dismissal instant and HA treats its update as a
  completion event. Failed/offline delivery retains that timestamp across
  restart. Preserve heartbeat/ack/failure-latch retry behavior.
- Prove legacy naps/no-anchor paths are unchanged **byte-for-byte**: configured
  first ramp, nine-minute snooze, maximum two snoozes, fifteen-minute terminal
  timeout, HA stop works. Do not apply commute timeout/re-ring/final policies.
- Verify worker/cache regressions: independent audio timing under blocked HA,
  serial commands and bounded subprocess calls, restoration after stop/failure,
  no post-restore worker write, failed start does not acknowledge playback,
  and restart/healthy idle HTTP does not clear the failure latch.

## Private infra contract follow-up — pending, NO deployment

This is an exact coordination checklist for the separate infra owner. No infra
files, running HA, devices or deployed services are changed by this docs lane.

1. `script.commute_publish_alarm` accepts **`deadline_at`**, stores offset-bearing
   UTC text in **`input_text.commute_alarm_deadline_utc`**, and clears a missing
   anchor to epoch zero. Replace the departure contract together with Python;
   the old helper is not consumed after upgrade.
   `input_text.commute_alarm_window_done_utc` carries the offset-bearing UTC
   dismissal instant sent by `PublishWindowDone` (completion timestamp now),
   not the original wake. HA treats its update as a completion event; the
   original wake remains in local done history for dedupe. Accept completion
   only at/after the current session's wake and not in the future; delayed
   retries must not restore lights or clear notifications for a newer session.
2. Fade fairy lights from **L−47**, reaching **full warm by L−37**. Save
   **`switch.fairy_lights_sync_receive`** and
   **`select.fairy_lights_live_override`**, disable sync, then restore those
   saved values on the `window_done` completion event. At first ring turn on
   **`light.smart_led_bulb`**.
3. Every transition into ringing sends a **silent** actionable notification via
   **`notify.mobile_app_nougat`**. **Snooze** presses
   **`input_button.commute_alarm_snooze`**. Show **Took meds** only while
   **`input_boolean.morning_meds_taken`** is off; toggle it once, guarding
   repeated/stale action delivery so it never toggles back. Clear the
   notification on the `window_done` completion event.
4. HA stop maps to five-minute snooze capped at L before final, and is ignored
   in final. Phone actions cannot bypass that phase gate.
5. Leave existing iOS Clock and critical alerts untouched. The compatibility of
   those layers with the silent intent is **OPEN for Finn's decision**.

## Activation gates (future operator work, not deployment authorization)

Only after coordinated infra migration and fresh source validation:

1. Deliberately update the private infra input and optional daemon/publish
   configuration. Confirm the enabled/master settings and HA contract together;
   repository edits alone are not deployment. Evaluate/render before applying.
2. Confirm `alarm publish --dry-run` and `alarm status --json` show expected
   future wake and buffered deadline, including epoch-zero clearing. Confirm
   the sink is the physical speakers, not Snapcast.
3. With the user present and warned, test a short legacy nap: safe starting
   volume, ramp/loop, restore mute/volume, desktop and HA stop, nine-minute
   snooze/max-two and fifteen-minute terminal cap (shortened test settings if
   needed). Software percent does not measure physical loudness.
4. Test the commute window with shortened timing: L−47 lights fade, L−37 first
   ring/bulb, repeated five-minute capped snoozes and timeout auto-snooze,
   20%/30 s/60 s re-rings, L final and no restart when already ringing, final
   no-stop/no-snooze/no-timeout while continuously live and audible, and desktop
   dismissal. Check original-wake done-history dedupe, dismissal-timestamp
   completion events, fairy-light saved-state restoration and notification clearing.
5. Test each silent ringing notification and actions, including meds absent
   after first successful tap. Confirm HA stop only snoozes before final and
   cannot dismiss final. Check activity-monitor warning and off/skip/clear
   escape paths before relying on a continuously live audible unbounded final.
6. Simulate audio failure/disconnected daemon and verify the existing once-only
   fallback without a notification storm; restore settings afterward. Verify
   worker independence, cache recovery and failure latch under HA outage.
7. Build/run the phone Shortcut and automation, compare confirmation to the
   published wake, and verify NYC/timezone behavior. Do not change iOS Clock or
   critical alerts pending Finn's silent-intent compatibility decision. Keep a
   manually set backup alarm until desktop and phone have demonstrably worked.

## Limits

A stopped desktop or unplugged speakers cannot be made audible by software.
A healthy process cannot prove a cable, amplifier or speaker works. Offline HA
prevents new naps and remote stop/skip changes; link may ring cached state.
Only a continuously live audible final is intentionally unbounded. After a
restart, every cached window state (including previously final ringing) expires
beyond L+10 min. Without an activity monitor, normal desktop dismissal is
unavailable and off/explicit skip/clear is needed.
No claim of alarm-grade reliability until source and hardware/device checks
pass. **Coordinated infra migration and live verification remain pending;
no deployment is performed here.**
