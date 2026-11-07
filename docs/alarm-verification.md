# Alarm verification and activation

## Shared prep advance and fallback window (2026-11-07): offline validation passed

The agreed contract is in [alarm-design.md](alarm-design.md#calendar-and-fallback-wake-window-revised-2026-11-07).
Background validators checked the reconciled source/tests in the isolated
`fix/fallback-wake-window` worktree based on `6e8bb77`. All commands below exited 0:

| Command | Result |
|---|---|
| `nix develop . --command pytest -q --cov=commutecompass --cov-report=term-missing --cov-fail-under=80` | 2533 passed, 37 warnings; 91.46% coverage (minimum 80%) |
| `nix develop . --command ruff check .` | All checks passed |
| `nix develop . --command mypy src tests` | No issues in 86 source files |
| `nix flake check --print-build-logs` | Passed, including sandboxed mypy/pytest/ruff |
| `nix build .#packages.x86_64-linux.default --print-build-logs` | Passed, including Python import check |

Checks used `agent-run-long` with 30-minute limits. Full local logs are under
`/tmp/opencode/agent-run-long.fallback-window-` with suffixes
`pytest-final.l4T3ArdMrl/output.log`, `ruff-final.3DPwJjwsYH/output.log`,
`mypy-final.TxzhoB2pDH/output.log`, `flake-final.PnPc9l9OSw/output.log` and
`build-final.kgGVz8fBw5/output.log`. Initial validation identified test expectation,
import and typing issues; these were corrected before the passing rerun.
Nix omitted incompatible systems; this is x86_64-linux evidence only.
Documentation-only result recording followed these checks. No HA writes, restart,
playback, push or deployment occurred. Historical results below are **not**
evidence for this revision, and offline tests do not prove live audibility.

Acceptance requirements covered by the tests and source reconciliation:

- `alarm.window.prep_advance_minutes` is canonical, integer 0–1440, default 0.
  `calendar_advance_minutes` is a legacy alias for the same value for both
  calendar and fallback. Equal dual keys are accepted; conflicting values are
  rejected. Effective TOML/JSON output and writes use the canonical key.
  `config set` through either name updates one setting; `config unset` through
  either name removes both spellings and restores 0. Reset also removes the
  shared override. Verify legacy-only files, duplicate aliases, invalid bounds,
  conflict handling and unchanged unrelated settings.
- With the window enabled, base final B is healthy buffered `leave_at` or the
  configured `alarm.fallback_time` on its NYC logical day. Effective final L is
  B minus the shared advance once; first wake W is L minus the configured first
  lead. Keep `deadline_lead_minutes = 0`. `prep.prep_minutes` remains route/event
  prep, not another alarm advance. The application first-lead default stays 37;
  the 60-minute integration choice is not a new default.
- For a noon fallback base, first lead 60 and deadline lead 0, expect the table
  below. An enabled empty-calendar day supplies a wake/deadline pair and opens
  the normal window; it is not a lone noon legacy alarm. Compare calendar and
  fallback **effective finals**. Include a calendar leave of 12:15: advance 15
  gives calendar final 12:00, so fallback final 11:45 wins even though the
  calendar first ring is before the noon base.
- Manual `alarm wake --leave 09:00` stays first 08:00 / final 09:00 with lead 60,
  regardless of advance. Earlier effective calendar finals still compete and
  `--force` still selects manual; fallback must not displace manual intent.
  Retained effective timestamps must not be advanced twice.
- Nap caps remain unadvanced (`leave_at - first lead` for healthy window plans,
  otherwise eligible `prep_at - wake lead`). Fallback does not manufacture a cap.
  Naps, prep-only wakes and disabled-window legacy timing/lifecycle remain
  separate from fallback windows. Route timing, buffers and pings do not change.
- Legacy helper payloads and v1 snapshots publish the same first/final pair.
  Enabled fallback uses existing v1 kind `commute`; no new kind or schema.
  Missing legacy deadlines still clear explicitly; an invalid v1 record is no
  new information, not an implicit legacy fallback.
- Feed fallback-generated pairs into fake-HA/fake-clock daemon tests: first ramp,
  unlimited capped snoozes, timeout auto-snooze, 20% hold/re-ring, continuous
  transition into final, desktop-only normal final dismissal, and the inclusive
  L+10-minute recovery cutoff. Preserve master-off, skip/clear, activity, audio
  failure latch, pending completion and active-session replacement protections.
- Preserve NYC logical-day ownership, UTC elapsed arithmetic across both DST
  folds and spring transition, and rollover when first wake crosses midnight or
  02:00. Planning failures remain degraded recovery with warning, not successful
  empty calendars: retain a valid future publication first, otherwise use the
  existing manual / next-logical-day safety fallback policy with window geometry.

| Noon base, 60-minute first lead, zero deadline lead | Advance 0 | Advance 15 |
|---|---|---|
| HA lights begin (published first minus 10 minutes) | 10:50 | 10:35 |
| Published first ring | 11:00 | 10:45 |
| Effective final / published deadline | 12:00 | 11:45 |

### Source reconciliation checkpoint — static inspection only

Current definitions match the shared geometry and alias contract:

- `config.py::AlarmWindowConfig` stores `prep_advance_minutes`, normalizes the
  legacy alias and rejects conflicting values. `_canonical_edit_key` makes set
  and unset target one value; the model-based TOML/JSON renderers emit the
  canonical name. Conflicting dual keys also make set/unset refuse without a
  write; the configuration owner must repair the conflicting source first.
- `alarm.py::_effective_deadline`, `_window_start` and `_fallback_window` apply
  the advance/first lead once, bypassing them for disabled fallback.
  `wake_for_day` compares effective finals when both candidates have windows;
  `_earliest_alarm_plan` opts out of advance for nap caps. `compute_wake` uses the
  same fallback geometry for degraded recovery. Retention preserves effective
  timestamps and calendar/fallback provenance for manual conflict decisions.
  `deadline_source` is internal persisted provenance only, not an HA wire field.
- `alarm.py::build_payload` and `build_schedule` preserve the legacy/v1 wire
  formats; a deadline selects existing v1 kind `commute`. The daemon's
  `_commute_deadline` admits a window by exact geometry, and `_window_step`
  supplies the existing lifecycle regardless of calendar/fallback origin.
- `tests/test_config.py` defines canonical/alias round trips, cross-spelling
  set/unset, equal/conflicting aliases and bounds. In
  `tests/test_alarm_calendar_advance.py`,
  `test_fallback_normal_window_publication_and_daemon_restart` covers both
  contracts and both 0/15 advances with the worked lights/first/final times;
  `test_calendar_fallback_selection_compares_effective_finals` covers competition.
  Further cases cover manual force, retained fallback, degraded recovery, nap
  caps, DST, and disabled fallback. The old unchanged-fallback case now applies
  only to `disabled_fallback`.
- Existing lifecycle/recovery cases remain in `tests/test_alarm_window.py` and
  `tests/test_alarm_recovery.py`; contract cases in `tests/test_alarm.py` and
  `tests/test_alarm_contract.py`. These definitions are not executed results.

Static inspection alone does not establish a pass. The fresh validation results
above establish offline behavior for the reconciled source/tests; no rendered
HA result, live-device delivery or audible outcome is inferred.

### Separate infra integration — not executed

The [integration settings and ownership](alarm-design.md#infra-integration-guidance--separate-work-not-executed)
are separate follow-up work: roll the application revision and, with approval,
merge `alarm.fallback_time = "12:00"` if desired plus window `enabled = true`,
`prep_advance_minutes = 15`, `first_alarm_lead_minutes = 60`, and
`deadline_lead_minutes = 0`. Fallback time belongs under `[alarm]`, not
`[alarm.window]`. A private legacy advance must be removed/migrated or equal to
15 in the merged configuration; no private configuration was inspected here.

Supplied read-only infra inspection reports the link overlay at
`modules/link/commutecompass.nix:54–70` already sets alarm enabled, legacy
publication and the 60-minute first lead from `lib/commute-alarm.nix`.
`modules/iot/commutecompass-wake-window.nix:123–128,459` validates that same
wake/deadline spacing, starts pending ten minutes before wake, and derives fade
timing from the published timestamps. That consumer needs **no light change or
second advance**. These are supplied source observations, not evaluated/rendered
or deployed results. No infra edits or live actions were performed.

## Opt-in HA contract repair

The additive implementation contract is [alarm-ha-contract.md](alarm-ha-contract.md).
Its evidence must include exact serialized length (including microseconds and
nulls), publication ordering/conflict/idempotency plus exact readback, HA restart
republish, same-mode final transitions, session-keyed terminal snapshots, player
failure, and fail-closed v1 schedule adoption. Slow/failing optional status export
must not reset audio timing or prevent master-off/skip/desktop dismissal. Offline
mocked tests do not establish real HA rendering, delivery, or audibility. Private
infra remains unedited and unactivated; its owner must review all consumers and
render the versioned script before any separately authorized migration.

## Claims and evidence owners

- Publisher lane: enabled calendar and fallback windows use L = base final
  minus the one shared prep advance, first wake L minus first lead, and final L
  with deadline lead 0. Prep-only and window-disabled waking stay legacy; nap
  caps stay unadvanced, with NYC/DST correctness. Replanning must not substitute
  an elapsed wake for a future one or reapply the advance to retained timestamps.
  Calendar/planning failure must remain explicit degraded recovery, not a
  successful empty-calendar result. Evidence required: pure computation and
  mocked jobs tests.
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
- Orchestrator after writers reconcile: delegated pytest/ruff/mypy checks and
  separate Nix checks/build passed as recorded above. Source/test evidence remains
  distinct from historical runs below and from unexecuted infra activation.

## Changes to earlier design notes

- The route-departure D−50 / D−13 design is superseded by **L−first lead / L**,
  where **L = base final minus shared prep advance**. Calendar bases use buffered
  `Plan.leave_at`; fallback bases use `alarm.fallback_time`. Safety/weather/
  realtime buffers already move the calendar base earlier; `Plan.depart_at` is
  informational only. First lead defaults to 37, shared advance to 0.
- Legacy `alarm.build_payload` sends **`wake_at`, `next_prep_at`, `next_event`**;
  `next_prep_at` is empty if absent. Window-enabled or bound-manual payloads add
  **`deadline_at`** (empty only for a wake with no window anchor). An enabled
  fallback now has a real deadline. No `prep_at`, `logical_date`, `summary` or
  `has_events` arguments are sent; the wire schema is unchanged.
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

## Historical revised wake-window evidence (2026-11-03)

Previously recorded validation for the L−37 / L revision, with no live HA writes
or audio. These results predate shared prep advance and enabled fallback windows;
they do **not** validate the 2026-11-07 revision:

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

The recorded suite covered the original cases in `tests/test_alarm.py`,
`tests/test_alarm_window.py`, `tests/test_alarm_ramp.py` and planner/routing tests.
The source-derived expectations below must be rechecked for the current revision;
new fallback/alias cases are listed above, not included in that historical pass.

### Door-leave anchor: historical pass and current expectations

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
- With advance 0 and first lead 37, verify that L equals **1746864000 minus all
  buffers**, `deadline_at` publishes L, first ring is L−37, and lights start L−47.
  With a nonzero shared prep advance, subtract that once more from the base
  `leave_at` to form L, not from the route or nap cap. Increasing safety/weather/
  realtime buffer must move the complete calendar window earlier by exactly
  that increment, without double subtraction. Do not anchor to transit boarding,
  event start, or informational `Plan.depart_at`.

### Publisher, lifecycle and regression checklist

- Publication/config migration: `deadline_at`, deadline UTC entity, first/final
  leads 37/0 by default, UTC folds and epoch-zero clear; old departure helper
  not consumed. Only exact matching wake/deadline pairs enable the window.
  Enabled fallback days supply a new matching pair, not yesterday's anchor or
  an empty deadline. Prep-only wakes still clear an absent anchor. Planning
  failure retains valid future state or uses explicit warned safety recovery.
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
- Prove naps, prep-only wakes and disabled-window legacy paths are unchanged:
  configured first ramp, nine-minute snooze, maximum two snoozes, fifteen-minute
  terminal timeout, HA stop works. Enabled fallback wakes are windows and must
  instead use the same timeout/re-ring/final policies as calendar windows.
- Verify worker/cache regressions: independent audio timing under blocked HA,
  serial commands and bounded subprocess calls, restoration after stop/failure,
  no post-restore worker write, failed start does not acknowledge playback,
  and restart/healthy idle HTTP does not clear the failure latch.

## Private infra contract follow-up — pending, NO deployment

This is a consumer coordination checklist for the separate infra owner to verify
or migrate where not yet satisfied. Supplied source inspection already confirms
the 60-minute light geometry; no additional light change is needed for shared
advance. No infra files, running HA, devices or deployed services are changed by
this docs lane.

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
2. Fade fairy lights from **published wake−10 minutes**, reaching **full warm at
   the published first wake** (L−47 / L−37 for lead 37; L−70 / L−60 for lead 60).
   Apply no extra prep advance in HA. Save **`switch.fairy_lights_sync_receive`** and
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
2. With separately authorized configuration and provider access, use
   `commutecompass alarm publish --dry-run` to inspect the future first wake and
   effective final (after the advance), including fallback geometry. Dry-run
   skips HA publication but can read calendar/routing providers. Compare the
   selected contract's HA schedule separately; `commutecompass alarm status
   --json` is a legacy-helper diagnostic, not a v1 playback acknowledgement.
   Check explicit epoch-zero clearing only when there is no window anchor.
   Confirm the sink is the physical speakers, not Snapcast.
3. With the user present and warned, test a short legacy nap: safe starting
   volume, ramp/loop, restore mute/volume, desktop and HA stop, nine-minute
   snooze/max-two and fifteen-minute terminal cap (shortened test settings if
   needed). Software percent does not measure physical loudness.
4. Only with explicit playback authorization and the user present, test both
   calendar and fallback windows with controlled timing: lights ten minutes
   before published first wake, first ring/bulb, repeated five-minute capped
   snoozes and timeout auto-snooze, 20%/30 s/60 s re-rings, L final and no restart
   when already ringing. Verify final no-stop/no-snooze/no-timeout while
   continuously live and audible, and desktop dismissal. Check original-wake
   done-history dedupe, dismissal-timestamp completion events, fairy-light
   saved-state restoration and notification clearing. Verify the noon / 15 / 60
   / 0 expected geometry above before any audible trial.
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
