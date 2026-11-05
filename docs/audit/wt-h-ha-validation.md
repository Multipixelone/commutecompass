# WT-H — Validate Home Assistant geographic data

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-ha-geometry` / `fix/audit-ha-geometry`.
**Exclusive write scope:** `src/commutecompass/models.py`, `src/commutecompass/ha_client.py`,
`tests/test_models.py`, `tests/test_ha_client.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## ALM-10 — P3 — invalid geographic values enter presence calculations

**Confirmed source/model reproduction:** `CurrentLocation` and `ZoneInfo` at
`models.py:312-328` accept NaN, out-of-range latitude/longitude and negative radius.
`ha_client.py:75-89` checks only numeric types; NaN accuracy bypasses the
`accuracy_m > min_accuracy_m` rejection. Zone ingestion at `:255-275` has no finite
or geographic bounds either. This is boundary/model hardening, not a proven current
poll presence-suppression bug: `jobs/poll.py:58-75` already guards invalid coordinates
and accuracy, and zone radius currently has no distance-calculation consumer.
Preserve those consumer guards and unknown accuracy as `None`. Stored invalid
locations already have a safe deserialization boundary (`store.py:1106-1120`);
no store rewrite or migration is established as necessary.

### Fix boundary

Require finite latitude in [-90, 90], longitude in [-180, 180], and nonnegative
finite zone radius/optional accuracy. Reject bools as numeric coordinates at ingestion.
Use consistent model constraints and boundary validation so malformed device data
returns `None`, and malformed zones are skipped individually rather than aborting
all zones or raising through a job. Catch expected model validation errors at the
client boundary. Preserve timezone-aware timestamps and the behavior for legitimate
unknown accuracy. Do not change `Origin`, public config schemas, thresholds, alarm
scheduling, or planner behavior incidentally.

### Regression tests / acceptance

- Direct model creation rejects NaN, infinities, out-of-range coordinates and negative
  accuracy/radius; boundary coordinate values and legitimate zero radius pass.
- Mocked HA location payloads with invalid coordinates/accuracy (including NaN with
  an active accuracy threshold, huge ints and bools) return the safe unavailable result.
- Mixed valid/invalid zone payloads retain valid zones and skip only invalid entries.
- Valid location and accuracy filtering still work; no uncaught validation or numeric
  conversion exceptions. Use synthetic fixtures, not personal coordinates/live HA.

**Focused command:** `pytest -q tests/test_models.py tests/test_ha_client.py`.
**Suggested commit:** `fix(ha): reject invalid location and zone geometry`.

This lane is the only writer of `models.py`; [WT-E](wt-e-alarm.md) owns daemon callback
isolation and must not edit it. After merging both, the integration owner checks
presence/poll/tomorrow regressions as part of the final suite.
