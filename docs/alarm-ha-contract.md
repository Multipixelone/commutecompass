# Alarm HA contract v1 (opt-in; not deployed)

This is the relay contract for the infra owner, not production HA configuration.
No activation, infra pin change, notification cadence change, or live-device
verification is implied. Existing installations default to `legacy`.

## Two records, separate authorities

- `input_text.commute_alarm_schedule_v1`: one atomic **published schedule**.
- `sensor.commute_alarm_status`: one atomic **advisory daemon observation**,
  including its locally locked session. Never use status as a control input to
  the daemon or as a replacement for master-off, skip, or desktop dismissal.

A newly published tomorrow schedule does not replace today's active locked
session. A later replan cannot extend that session's deadline. The daemon's
status reports the effective lock, not a copy of the schedule helper.

## Schedule wire format

The versioned script takes `snapshot` (a JSON **string**, not a mapping) and
`next_event` (display-only text). Example service data:

```json
{
  "snapshot": "{\"v\":1,\"id\":\"6b6e9c724ace4c81a350ef144a7ef39b\",\"issued_at\":\"2026-11-04T01:00:00Z\",\"kind\":\"commute\",\"wake_at\":\"2026-11-04T12:23:00Z\",\"deadline_at\":\"2026-11-04T13:00:00Z\",\"next_prep_at\":\"2026-11-04T12:00:00Z\"}",
  "next_event": "Example event"
}
```

The canonical string uses the displayed key order, no separator whitespace,
ASCII JSON, and UTC `Z` datetimes, with six microsecond digits when nonzero.
The publisher confirms the helper contains the **exact string** after the call;
HTTP success alone is not acceptance. Retry the same publication with the same
ID, issue time, and body, rather than generating a new identity per HTTP attempt.

Kinds:

| Kind | Wake | Deadline | Next prep |
|---|---|---|---|
| `commute` | required | required; wake = deadline − configured first lead | optional |
| `legacy` | required | null | optional |
| `none` | null | null | null |

`next_prep_at` retains its existing publisher meaning despite the historical
name: it is the effective next first-alarm / nap-cap instant, not necessarily a
plan's raw `prep_at`. Do not subtract the wake lead again.

Only a validated `none` clears a v1 wake. A missing/invalid/unavailable snapshot
or contract selector mismatch means **no new information**, not cancellation,
not a genuine no-events result, and never permission to read legacy wake,
deadline, prep, or the wake portion of a legacy next-alarm sensor. Nap controls,
buttons, skip and master-off remain independent.

The longest record is 235 characters with all three schedule timestamps and
`issued_at` containing microseconds. Event titles are deliberately outside it.
The 255-character limit applies to the actual serialized helper state. Do not
truncate or spread a record across helpers. Exact received-string lengths are
covered for every valid kind/null combination:

| Kind | `next_prep_at` | Whole-second length | With microseconds |
|---|---|---:|---:|
| `commute` | null | 189 | 210 |
| `commute` | filled | 207 | 235 |
| `legacy` | null | 170 | 184 |
| `legacy` | filled | 188 | 209 |
| `none` | null (required) | 150 | 157 |

### HA declarations and versioned script example

The following is a documented example for a Nix-managed HA package. Do not paste
it into a running installation without the infra consumer review and explicit
activation approval. Omit `initial` so helper state restores on HA restart.
The example assumes the approved 37-minute first lead; change both producers
and validation together if that setting is deliberately changed.

```yaml
input_text:
  commute_alarm_schedule_v1:
    name: Commute alarm schedule v1
    max: 255

input_select:
  commute_alarm_contract:
    name: Commute alarm contract
    options:
      - legacy
      - v1

script:
  commute_publish_alarm_v1:
    alias: Commute publish alarm v1
    mode: queued
    max: 10
    fields:
      snapshot:
        required: true
        selector:
          text: {}
      next_event:
        required: false
        selector:
          text: {}
    sequence:
      - variables:
          incoming: "{{ snapshot | from_json(none) }}"
          # Keep the raw text nested: HA may native-coerce a bare JSON string.
          current: "{{ {'raw': states('input_text.commute_alarm_schedule_v1')} }}"
      - variables:
          valid: >-
            {% set all = namespace(ok=true) %}
            {% for raw in [snapshot, current.raw] %}
            {% if loop.first or raw not in ['', 'unknown', 'unavailable'] %}
            {% set p = raw | from_json(none) %}
            {% set keys = ['v','id','issued_at','kind','wake_at','deadline_at','next_prep_at'] %}
            {% set ts = '^\\d{4}-\\d{2}-\\d{2}T([01]\\d|2[0-3]):[0-5]\\d:[0-5]\\d(\\.\\d{6})?Z$' %}
            {% set ns = namespace(ok=p is mapping and raw is string and raw | length <= 255) %}
            {% if ns.ok %}
              {% set ns.ok = p.keys() | list | sort == keys | sort
                 and p.v is integer and p.v == 1
                 and p.id is string and p.id | length == 32
                 and p.id is match('^[0-9a-f]{32}$')
                 and p.kind in ['commute','legacy','none'] %}
              {% for key in ['issued_at','wake_at','deadline_at','next_prep_at'] %}
                {% set value = p.get(key) %}
                {% if value is not none or key == 'issued_at' %}
                  {% if value is not string or value | length not in [20,27]
                        or not value is match(ts) or as_datetime(value, none) is none %}
                    {% set ns.ok = false %}
                  {% endif %}
                {% endif %}
              {% endfor %}
              {% if ns.ok %}
                {% set canonical = dict(v=p.v, id=p.id, issued_at=p.issued_at,
                   kind=p.kind, wake_at=p.wake_at, deadline_at=p.deadline_at,
                   next_prep_at=p.next_prep_at) | to_json
                   | replace(', ', ',') | replace(': ', ':') %}
                {% set ns.ok = raw == canonical and '.000000Z' not in raw %}
              {% endif %}
              {% if p.kind == 'none' %}
                {% set ns.ok = ns.ok and p.wake_at is none and p.deadline_at is none and p.next_prep_at is none %}
              {% elif p.kind == 'legacy' %}
                {% set ns.ok = ns.ok and p.wake_at is not none and p.deadline_at is none %}
              {% elif p.kind == 'commute' %}
                {% if ns.ok and p.wake_at is not none and p.deadline_at is not none %}
                  {% set ns.ok = (as_datetime(p.deadline_at) - as_datetime(p.wake_at)).total_seconds() == 2220 %}
                {% else %}
                  {% set ns.ok = false %}
                {% endif %}
              {% endif %}
            {% endif %}
            {% set all.ok = all.ok and ns.ok %}
            {% endif %}
            {% endfor %}
            {{ all.ok }}
      - if: "{{ not valid }}"
        then:
          - stop: Invalid alarm schedule v1
            error: true
      - variables:
          old: "{{ current.raw | from_json(none) }}"
      - if: "{{ old is mapping and old.get('id') == incoming.id and current.raw == snapshot }}"
        then:
          - stop: Already committed
      - if: >-
          {{ current.raw not in ['', 'unknown', 'unavailable'] and
             (old is not mapping or old.get('v') != 1 or
              as_datetime(old.get('issued_at'), none) is none or
              old.get('id') == incoming.id or
              as_datetime(incoming.issued_at) <= as_datetime(old.get('issued_at'))) }}
        then:
          - stop: Stale or conflicting alarm publication
            error: true
      # The ONLY authoritative commit. No schedule field was written above.
      - action: input_text.set_value
        target:
          entity_id: input_text.commute_alarm_schedule_v1
        data:
          value: "{{ snapshot }}"
      # Existing compatibility mirror actions may follow here. They are not
      # v1 schedule inputs and their partial failure must not undo this commit.
```

Before the single write, reject malformed field types/unknown fields, invalid
canonical UTC timestamps, bad commute tuples, and overlong payloads. Preserve
microseconds; compare ordering as instants, not lexicographic ISO strings.
Same ID plus exact content is an idempotent success. Same ID/different content,
an older issue time, or different content at equal issue time is a conflict.
The script is queued so check-and-commit invocations do not interleave. Manual
helper editing or another writer would invalidate that single-writer guarantee.
A malformed nonempty previous record requires operator repair, not blind replace.

The old publication script must reject schedule writes while the selector is
`v1`. All HA consumers of wake/deadline/prep (including lights, phone selection,
and fallback eligibility) must branch on this selector and parse the same whole
record; missing v1 state must not cause automatic legacy fallback. Compatibility
mirrors remain for older readers only. Review every consumer before activation.

Crash before the single write leaves the old complete schedule. Crash after it
leaves the new complete schedule even when all compatibility writes are missing.
Do not describe queued multi-helper writes as atomic.

## Status wire format and consumer rules

The daemon optionally POSTs one body to
`/api/states/sensor.commute_alarm_status`. Do not declare a template/helper with
the same entity ID: the REST-created sensor has no YAML declaration and is
recreated by periodic daemon export after HA restart. State is only `idle`,
`ringing`, or `snoozed`; structured data lives in attributes, not the 255-character
state. Enable export explicitly with `[alarm] status_entity =
"sensor.commute_alarm_status"`; the empty default sends no new status traffic.

```json
{
  "state": "ringing",
  "attributes": {
    "schema": 1,
    "revision": 12,
    "observed_at": "2026-11-04T13:01:00Z",
    "session": {
      "id": "34d3d3db0cba4eedb39eb9986f71d7d7",
      "original_at": "2026-11-04T12:23:00Z",
      "kind": "wake",
      "phase": "final",
      "locked_deadline_at": "2026-11-04T13:00:00Z"
    },
    "ring": {
      "id": "6cdd469b45c149368f0ff5eb47591efa",
      "started_at": "2026-11-04T12:58:00Z",
      "playback": "running",
      "checked_at": "2026-11-04T13:01:00Z"
    },
    "audio_failed": false,
    "snooze_until": null,
    "ended": {"wake": null, "nap": null}
  }
}
```

`revision` is a cache-persisted semantic revision, not an HA delivery counter.
`observed_at` is refreshed on every export; `checked_at` is the most recent
local player check. Session ID remains stable over snoozes, earlier deadline
locks, continuous final, and cache recovery. Each logical new ring gets a ring
ID; continuous ringing across L retains its ID and start time. Playback is
`starting`, `running`, `stopped`, or `failed`, never inferred just from mode.
On restart a cached running claim is not reused as current player health.

A terminal entry contains `session_id`, `original_at`, `kind`, `phase`,
`locked_deadline_at`, `ended_at`, and `reason`. `session` is null when idle;
`ended` retains only the latest wake and latest nap. Audio failure is not
completion. Export always builds the latest complete local snapshot rather than
retrying an old body after a newer session begins.

Status is advisory. Export is attempted once per successful-input poll, after
local transitions and audio actions, with an explicit 0.5-second HTTP timeout
and no immediate retry. A failure does not stop local audio, undo an already
applied authoritative cancellation, clear the active lock, or reset the ramp.

This synchronous request is **not nonblocking**. It can postpone observation of
a control arriving after the current input read until the next poll. The HTTP
timeout bounds network phase/inactivity waits (connect, read, write and pool),
not an absolute end-to-end 0.5-second deadline; DNS/OS scheduling and a peer
trickling response headers prevent a strict wall-clock guarantee. The streaming
response is closed without consuming a body. Under ordinary HA timeout failure,
this adds one short failed request to existing poll/read/reconciliation latency;
there is no status retry loop. Existing audio progression runs independently.
A strict total stop-latency guarantee would require a separate transport/timing
change and is not claimed here.
`ringing` alone is not playback evidence. Only a current matching session and
ring with successful player health can acknowledge playback. Health is process
and worker observation, not proof of human audibility. The existing `alarm status`
CLI remains a legacy-helper diagnostic, not a v1 playback acknowledgement or
an authorization source.

Consumers must reject invalid, future or stale `observed_at` on every use. Use
`max(90 seconds, 3 * poll_seconds)` as the freshness budget; do not use entity
`last_changed`. A final remains valid indefinitely only while observations keep
refreshing within that budget: the L+10 recovery cutoff is not a live-status
expiry. Status never grants permission to press a button or issue a notification
merely because its timestamp is newer than a helper. Do not change existing
fallback cadence as part of this contract.

For actionable ringing UI/notifications require a fresh observation, a non-null
current session and ring, `state == "ringing"`, `ring.playback == "running"`,
and `audio_failed == false`; match the session/ring identity before acting on an
older rendered notification. `ringing` alone is scheduling intent, not player
health. `starting`, `failed`, `stopped`, missing or stale observations are not
evidence of running playback. Snooze is available only in the appropriate
legacy/window phase, never final. Final dismissal remains local desktop activity
(or independent master-off/skip), not an HA stop button. Advisory status must
never block those independent local safety controls.

A terminal record applies only to its session ID. A late wake terminal cannot
cancel a newer wake, and a nap start/termination cannot acknowledge a wake.
Keep only latest per-kind terminal snapshots, not a generic event history.

## Migration and invariants

Keep Python and HA in legacy mode by default. The additive Python settings are:

```toml
[alarm]
schedule_contract = "legacy"  # opt in to "v1" only with coordinated HA readiness
schedule_entity = "input_text.commute_alarm_schedule_v1"
contract_entity = "input_select.commute_alarm_contract"
publish_v1_script = "script.commute_publish_alarm_v1"
status_entity = ""  # optional: "sensor.commute_alarm_status"
```

Stage the new helper/script and snapshot, verify exact readback, then coordinate
Python `schedule_contract = "v1"` and selector `v1` while idle and away from an
alarm boundary. Activation
and rollback require separate authorization. Missing entities are not rollback.
No upstream publication or infra pin change is authorized by this implementation.

L is buffered `Plan.leave_at`: lights L−47, first L−37, final L. Unlimited
five-minute window snoozes cap at L; re-rings use 20%/30-second hold/60-second
ramp. Final has no timeout and ignores HA stop/snooze; desktop dismissal,
master-off and skip retain their existing meaning. Naps remain legacy. A
continuously live final may ring past L+10; only restart recovery is cut off
there. No overlap precedence or medication behavior is changed.
