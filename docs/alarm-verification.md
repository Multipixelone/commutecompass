# Alarm verification and activation

## Claims and evidence owners

- Publisher lane: future morning wake is the earlier of commute prep minus wake lead and noon (configurable), with NYC/DST correctness; naps have a future prep-minus-lead cap. Replanning never replaces tomorrow with an elapsed wake. Calendar/planning failure never masquerades as a safe no-events fallback. Evidence: pure computation + mocked jobs tests.
- Daemon lane: real HA future-only sensor transitions do not swallow deadlines; skip/off are authoritative even across cached outages; stop/snooze/timeouts and restart state work; audio failure is visible to fallback. Evidence: fake HA snapshots and fake audio/subprocess tests, cache reload scenarios.
- HA lane: helpers, scripts, templates and notifications share the Python contract. Fallback sends once if desktop is offline or playback fails; no periodic notification storm. Evidence: evaluated Nix host config, rendered YAML, Jinja scenarios. Does not establish live-device delivery.
- Orchestrator after writers reconcile: inspect artifacts, run full pytest/ruff/mypy once in one delegated validation lane; evaluate package/module integration separately. Existing unrelated baseline failures are reported, not hidden.

## Changes to earlier design notes

- Keyboard/mouse stop **is enabled**: the desktop is out of reach of bed. The earlier no-keyboard-stop text was a transcription error, not a user decision.
- Heartbeat proves process connectivity, not functioning sound. Fallback also requires an acknowledgement of the specific deadline after successful playback start; `input_boolean.commute_alarm_audio_failed` overrides it on playback failure. This is one assessment in the first three minutes, not continuous delivery monitoring.
- Offset-bearing UTC text helpers are authoritative for wake, nap, skip, cap, heartbeat and playback acknowledgement. Local `input_datetime` helpers remain display/legacy values; they cannot preserve the intended instant by themselves through a fall-back DST fold and HA restart.
- Ordinary rollover retains a just-due wake through the fallback window and does not cancel active ringing/snooze. Interrupted playback resumes only within its original bounds. Explicit stop/off/skip still wins.
- The phone Shortcut must create and verify a replacement before deleting the old owned alarm, so a failed creation does not remove the only backup.
- iOS Clock alarms cannot be edited remotely by HA. Phone skip/off/replans only apply when the Shortcut runs; link follows HA immediately. Confirmation warns about divergence; it does not prove the phone will make sound.

## Source-validation result (2026-11-01)

- Full pytest: **944 passed, 10 failed**. The same ten poll-replanning test
  failures reproduce on unchanged `192a310`; they are baseline failures, not a
  clean suite. Their fixtures use today's date on the DST-transition day, but
  the definitive cause was not established. Ruff and strict mypy passed.
- Actual link module evaluation with a local package override and temporary
  alarm-enabled config passed; targeted package, daemon/publish units and timer
  built. This proves wiring, not activation or audible sound.
- Final reconciled checks: **262 targeted tests passed**, global Ruff passed and
  strict mypy passed (31 source files). The final daemon-only boundary correction
  then passed **78 daemon tests** and repeated global Ruff/mypy; unchanged areas
  retain the earlier integrated evidence. Publisher lane checks: 57 tests passed.
  These cover
  historical-day publication, due-wake rollover, interrupted playback, bounded
  snooze recovery, UTC folds, playback acknowledgement and activity reattachment.
- Rendered HA YAML parsing, 130 Jinja syntax checks and focused behavior scenarios
  passed, including authoritative DST-fold heartbeat, deadline retention, cap,
  skip, acknowledgement and cleanup. No live HA change or notification was made.
- The package/module wiring build predates the final lifecycle/UTC corrections;
  it establishes the unchanged unit/environment contract, not a final host build.
- Final bounded static safety gate passed for lifecycle restart/cleanup, no-cap
  sentinel and unknown UTC authority. Earlier fixes covered publication, rollover,
  fallback acknowledgement, DST and activity recovery. This is source approval,
  not device activation evidence.

## Activation gates (not automated by a unit test)

1. Update the infra commutecompass flake input to a revision containing this feature; enable the optional daemon and publish timer for link. Configure `[alarm].enabled = true` in the private configuration, sharing the existing HA credentials. Enable the HA master switch once. Apply iot and link configuration deliberately; do not assume repo commits are deployed.
2. Confirm `alarm publish --dry-run` and `alarm status --json` show the expected future time before enabling playback. Confirm the configured sink matches the physical speakers, not Snapcast. No live sound has been emitted by development tests.
3. Build the iOS Shortcut and its 21:00/charger automation on the phone. Grant HA critical notification permission and run a manual sync; compare `commute_alarm_confirmed` to the published wake. Ensure iPhone and HA interpret NYC times correctly, including travel/timezone changes.
4. With the user present and warned, set a short nap test: verify speaker ramp, looping, restore of previous mute/volume, keyboard/mouse stop, HA stop, snooze, and 15-minute cap (using shorter configured values if desired). Start at safe physical speaker volume; software percent cannot measure loudness.
5. Simulate audio failure / disconnected daemon with a near-future test deadline; verify exactly one critical alert. Restore services/settings afterward.
6. Keep a manually set phone alarm until both the desktop end-to-end test and phone alarm have demonstrably worked.

## Limits

A stopped desktop or unplugged speakers cannot be made audible by software. A healthy audio process does not prove a physical cable, amplifier, or speaker is working. Offline HA prevents new naps and remote stop/skip changes; link may still ring its last cached schedule. No claim of alarm-grade reliability until hardware/device checks pass.
