# Design: Directions → trusted GTFS identity bridge (P1)

Status: proposal, not implemented. Scope: the P1 item in
[realtime-followups.md](realtime-followups.md). This design keeps the completed
safety baseline intact; it adds a producer of validated identity and a NYCT
feed adapter. It does not change poll or padding contracts.

## 1. Current gap

- `routing.py:_parse_step` builds `TransitLeg` from `transit_details`. It sets
  `system`, `line` (`short_name` or `name`), `headsign`, `departure_stop` and
  `arrival_stop` names, and `scheduled_departure_valid`. It never sets any
  `gtfs_*` field, and it drops stop coordinates, `num_stops` and agencies.
- `models.py:TransitLeg` already has `gtfs_trip_id`, `gtfs_route_id`,
  `gtfs_start_date`, `gtfs_start_time`, `gtfs_boarding_stop_id`,
  `gtfs_boarding_stop_sequence` and `gtfs_direction_id`. All of them default
  to `None`, and no code path sets them.
- `realtime.py:_compute` → `_same_boarding` needs all identity fields to be
  present and equal to the prediction. Without them, every Directions plan
  returns `unmatched` ("no unambiguous scheduled boarding identity").
- **The feed side is also incomplete.** I captured live NYCT feeds on
  2026-10-31 (`gtfs`, `gtfs-ace`, `gtfs-nqrw`, `gtfs-l`). They show:
  - `StopTimeUpdate.stop_sequence` was absent in all 1,962 NQRW updates.
  - `departure.delay` and `arrival.delay` were absent; only `.time` was set.
  - `TripDescriptor.direction_id` was absent on every trip except in the L
    feed. There it was `0` even for `L..S` trips, while static `trips.txt`
    gives `L..S` `direction_id=1`. That value is **wrong** and must not be
    trusted.
  - Direction is carried by the trip_id shape (`..N`/`..S`), the platform
    suffix of `stop_id` (`R20N`), and `NyctTripDescriptor.direction`
    (extension 1001). In the NQRW capture that field was `3` (SOUTH) on all 82
    southbound trips and missing on the 74 northbound trips.
  - `trip_id` uses the NYCT short form `087600_N..S36R`, and `start_date` is
    `YYYYMMDD`. `start_time` (`14:35:46`) does **not** match the trip_id's
    origin time (`087600` = 14:36:00).

  So `realtime.py:_accumulate` produces predictions where `stop_sequence`,
  `delay` and `direction_id` are `None` (or untrustworthy), and
  `_same_boarding` and `_compute` reject them. Even with Directions-side
  identity, NYCT could never yield an `observed` result today. Both halves need
  work.
- Cache and estimate paths: `planner.py:plan_event` reuses
  `store.get_cached_route` (marked `approximate`/`from_cache`) or
  `routing.estimate_route`. `realtime.py:_compute` does not currently check
  `route.from_cache`/`approximate`. Its only protection is the
  `scheduled_departure_valid` and identity checks.

### What Directions actually provides (`DirectionsTransitDetails`)

These come from the Google Directions (Legacy) response reference:

| Field | Content | Bridge use |
|---|---|---|
| `line.short_name` / `line.name` | e.g. `"Q"`, `"C"` | candidate route_id set (allowlist) |
| `line.agencies[].name` | `"MTA New York City Transit"` | agency gate |
| `line.vehicle.type` | `SUBWAY` | mode gate |
| `departure_stop` / `arrival_stop` | `{name, location{lat,lng}}`, **no stop_id** | station gate (name + coords) |
| `departure_time` / `arrival_time` | `{value epoch s, text, time_zone}` | exact schedule join |
| `headsign` | free text, often the terminus | display only (see §5) |
| `num_stops` | stops after boarding, including the alighting stop | sequence cross-check |
| `headway`, `trip_short_name` | optional; usually unset for subway | ignored |

Directions provides no trip_id, route_id, stop_id, direction or service date.
Every identity field must therefore come from joining with static GTFS.

## 2. Trust and provenance model

A **validated GTFS context** is a complete identity tuple
(`trip_id` in RT form, `route_id`, `start_date` = service date,
`boarding_stop_id` = directional platform, `boarding_stop_sequence`,
`direction_id`). The bridge is its only producer. The bridge has exactly one
proof: a unique static-GTFS stop event that agrees exactly with the Directions
leg. The checks are in §4–5.

Serialization: add one optional nested model to `TransitLeg`:

```
gtfs_provenance: Optional[GtfsProvenance] = None
class GtfsProvenance(BaseModel):
    source: Literal["gtfs_bridge"]
    feed_kind: Literal["supplemented", "regular"]  # which static feed was imported
    feed_version: str        # feed_info.feed_version of the imported static feed
    feed_sha256: str         # hash of the downloaded zip
    validated_at: datetime   # tz-aware, UTC
```

Rules:

1. `realtime.py:_same_boarding` requires `gtfs_provenance` with a known
   `source`, in addition to the existing field checks. Legacy rows deserialize
   with `None`, so they stay unmatched. Existing tests that build bare
   `gtfs_*` legs must add provenance explicitly. That is intended: no identity
   counts as trusted without it.
2. The bridge sets either all identity fields plus provenance, or none of them.
   Pydantic defaults never fill in provenance, and no other code writes it.
3. Cache and estimate routes never carry identity. `planner.plan_event` strips
   `gtfs_*` and `gtfs_provenance` from a route marked `from_cache`/`approximate`.
   As defence in depth, `realtime._compute` returns `unmatched` for
   `route.from_cache or route.approximate`. A cached route's `start_date` is
   for a past day anyway. Do not depend on that.
4. Identity is never copied between plans. `jobs/poll.py:_same_journey` keeps
   excluding GTFS fields from journey identity, and `_retain_replan_safety`
   carries buffers, not identity. This keeps the rule in
   `poll-replan-safety.md` ("No GTFS identity is inferred or copied").
5. `store.py` needs no schema change. Plans are pydantic JSON
   (`upsert_plan`/`model_validate_json`), so provenance round-trips.

## 3. Stage-1 scope (subway allowlist)

The support set is a vendored, reviewed table
(`src/commutecompass/data/gtfs_bridge_allowlist.toml`). The bridge works only
where it has an explicit entry:

```
[[platform]]
route_ids = ["Q"]                # GTFS route_id; never "X" variants implicitly
directions_line = ["Q"]          # accepted line.short_name values, exact match
parent_station = "R20"           # 14 St-Union Sq (BMT Broadway)
platforms = { N = "R20N", S = "R20S" }
directions_names = ["14 St - Union Sq", "14 St-Union Sq"]   # exact, normalized
max_coord_distance_m = 150
```

- **Line → route_id.** Look up `line.short_name` (falling back to
  `line.name`) after uppercase and whitespace normalization, and require an
  exact allowlist match. Never use `rstrip("X")` or fuzzy matching. Express
  routes (`6X`, `7X`, `FX`) and shuttles (`GS`, `FS`, `H`) need their own
  entries. Also require `vehicle.type == "SUBWAY"` and an agency name
  containing `MTA New York City Transit`.
- **Boarding stop → platform.** `departure_stop.name` must match one of the
  entry's `directions_names` after normalization. Its `location` must also be
  within `max_coord_distance_m` of the static `stops.txt` parent coordinates.
  Both gates are required. The parent station limits candidates to the two
  platforms (`R20N`, `R20S`). The bridge never picks direction from the
  headsign. It picks it from the unique static trip (§4), whose `stop_times`
  row names the platform. The chosen platform must be one listed in the entry.
  Same-name complexes (14 St-Union Sq is `635` *and* `R20`; there are 76 names
  shared by more than three stops) are separated by route_id, the allowlisted
  parent and coordinates.
- **Direction.** The value comes from `trips.txt.direction_id` of the matched
  static trip. It must agree with the platform suffix (`N`→0, `S`→1) and with
  the shape segment of the trip_id (`..N`/`..S`). If any of these disagree,
  reject. On the RT side the direction is derived (§6) and never read from
  the feed's `direction_id`.
- **Service date.**
  - The static boarding time is `departure_time` in `stop_times.txt`. It is
    seconds after "noon minus 12h" of the service day, and it can be ≥24:00:00
    (18,844 such rows in the 2026-10 feed).
  - For a Directions epoch `T`, try candidate service days `D` ∈
    {local date of `T`, that date − 1}.
  - For each `D`, compute the instant as
    `datetime.combine(D, 12:00, NYC_TZ) − 12h + offset`. That is the GTFS
    definition: on DST-change days, noon is the stable anchor. Then require
    `|T − instant| ≤ 30 s` in UTC (D3). Exactly one trip on the route may fall
    inside that window; two or more candidates means rejection.
  - Service activity for `D` comes from `calendar.txt` (weekday flag and date
    range), overridden by `calendar_dates.txt` (1 = add, 2 = remove).
  - The result sets `gtfs_start_date = D.strftime("%Y%m%d")`. That is the
    NYCT RT convention, observed: `start_date` is the service day.
- **NYC DST.**
  - Fall-back hour (01:00–02:00 occurs twice): the noon-minus-12h anchor maps
    each offset to a single instant, so the two 01:30s stay distinct. Compare
    in UTC only.
  - Spring-forward: nominal times in the 02:00 gap resolve to a real instant
    via the anchor. If Google's epoch disagrees, there is no match. That fails
    closed.
- **Feed choice.** Use `gtfs_supplemented.zip`. It carries the 7-day
  service-change pick, for example `L0S8`, which is active on 2026-10-31. The
  regular `gtfs_subway.zip` had `feed_start_date=20261101` on 2026-10-31, so
  it did not cover that day at all. If the supplemented download or its
  validation fails, fall back to the regular feed (D5). The import records
  which feed it used, and the bridge only matches dates that feed covers.
- **Initial set (D1).** Both platforms at each of these stations. Everything
  else stays unmatched.
  - Utica Av (A/C; home). Expected parent `A48`.
  - W 4 St–Wash Sq, 8 Av level (A/C; work: Salt & Straw). Expected `A32`.
  - 47–50 Sts–Rockefeller Ctr (B/D/F/M; work: FAO Schwarz). Expected `D15`.

  These stop IDs are expected values. The importer must check each one against
  `stops.txt` (name and coordinates) and refuse to activate the allowlist if any
  check fails. The 6 Av level at W 4 St (`D20`) is not included, so a B/D/F/M
  leg that boards there stays unmatched.

## 4. Static match algorithm (bridge)

`gtfs_bridge.validate_leg(leg, payload_step, index) -> Optional[Identity]`.
It runs in `routing.py:_parse_step`, or just after it, for the first TRANSIT
leg only. That is the only leg the matcher uses. It needs:

1. `scheduled_departure_valid`, the allowlisted route, the station gate, and an
   index whose coverage contains `D` (see §5, stale index).
2. All static `stop_times` rows such that:
   - `stop_id` ∈ entry platforms and `trip.route_id` ∈ entry route_ids;
   - the service is active on `D`;
   - the absolute instant equals `T`.
3. **Exactly one** row, across both candidate `D`s and both platforms.
4. A `num_stops` cross-check (when present): the matched trip has a stop at
   `stop_sequence + num_stops`. Its parent name/coordinates must agree with
   `arrival_stop`, and its arrival instant must equal `transit_details.arrival_time`.
5. The RT trip_id is derived as the suffix after the first `_` of the static
   trip_id (`L0S8-N-2058-S17_087600_N..S36R` → `087600_N..S36R`). This needs
   the suffix to be unique among trips active on `D`. On 2026-10-31,
   `105100_H..S21R` was duplicated across `L0S8` and `L1S7`. Duplicates
   reject.
6. `gtfs_start_time` stays `None`. NYCT RT `start_time` disagrees with the
   static origin time (`14:35:46` vs `087600`→14:36:00), so equality would
   reject valid trips. Identity rests on trip_id + start_date.

The output fills all `gtfs_*` fields and `gtfs_provenance`. Any other outcome
yields no identity, plus a debug log line with the reason code.

## 5. Ambiguity and rejection rules (all fail closed → `unmatched`)

| Case | Rule |
|---|---|
| Multiple plausible trips (two trips at T, express+local on shared platform, duplicate RT suffix) | >1 static row or duplicate suffix → no identity |
| Same-name stations / complexes | name **and** coords **and** allowlisted parent; never pool parents |
| Opposite direction | platform suffix, static direction_id, shape `N/S`, and RT derived direction must all agree; headsign never decides |
| Directions time off-schedule (Google realtime-adjusted or rounded) | ±30 s UTC window (D3); exactly one candidate trip per route inside it, else reject |
| Cancellation | `trip_relationship == CANCELED` on the exact trip → `cancelled`, never 0-delay `observed` (existing) |
| Skipped boarding stop | exact trip, `stop_relationship == SKIPPED` → `skipped` (existing) |
| Trip absent from feed | `unmatched` / zero padding; absence is not "on time" |
| Stale/skewed feed, non-FULL_DATASET, partial feed failure | existing `_fresh`/`usable_feeds`/`feed_failed` semantics unchanged |
| Stale static index (no coverage for D, version older than N days, hash mismatch) | no identity produced |
| RT update at boarding platform missing `departure.time` | no measurement |
| Another trip's delay | `_same_boarding` exact equality; candidates from other trips never contribute; `len(candidates) != 1` → `unmatched` (existing) |

## 6. NYCT feed adapter (realtime side)

NYCT supplies neither `delay` nor `stop_sequence`. The adapter therefore
derives both from the **validated** leg, and only for that exact trip. It never
derives them across trips.

- **Trip.** `trip_id` and `start_date` must be equal, and `route_id` must be
  equal.
- **Direction.**
  - Derive it from the RT trip_id shape `..N`/`..S`.
  - Require the stop_id suffix to agree with it.
  - If `NyctTripDescriptor.direction` (extension 1001, field 3) is present,
    it must also agree.
  - Ignore `TripDescriptor.direction_id` entirely, because the L feed sends
    `0` for southbound.
- **Stop.**
  - Exactly one update for `stop_id == gtfs_boarding_stop_id` in that trip.
    The 2026-10 static feed has no trip that revisits a stop. If any trip
    did, reject it.
  - When the update carries `stop_sequence`, it must equal the validated
    value. When it is absent, the adapter supplies the leg's value, but only
    after the uniqueness check.
- **Delay.** `delay = departure.time − validated scheduled departure`.
  - Compute it in UTC seconds; never in wall-clock time.
  - Take `departure.time` from the update; use `arrival.time` only for
    "transit" stops whose two values are equal. That covers NYCT's
    transit-time convention.
  - The `_compute` timestamp cross-check becomes an identity once the delay is
    derived. Correctness therefore depends on the trip join, not on that
    check.
  - Negative delays clamp to 0 (existing behaviour).
- **Scope.** Gate this with a `system == "MTA Subway"` and provenance
  `source` check, and keep it inside `realtime.py`. Feeds that do supply
  `delay` continue to use the existing path.

The open safety question: a derived delay is a measurement only while the
train is being tracked. Before assignment, NYCT predictions are the schedule
(`NyctTripDescriptor.is_assigned` = false). Decision (D4): unassigned trips
count as `unmatched`, not as 0-minute `observed`.

## 7. Static GTFS acquisition options

| Option | Deps | Freshness | Size/cost | Risk |
|---|---|---|---|---|
| A. Vendored curated table (allowlisted stop_times rows checked into repo) | none | stale at every pick/supplement (weekly service changes, holidays) | small | silently wrong after GTFS change → must expire; requires release per pick |
| B. Download zip at runtime, parse in memory per process | stdlib `zipfile`/`csv` | fresh | supplemented zip ≈ 20 MB, `stop_times.txt` ≈ 143 MB / 2.4 M rows; seconds of CPU per run | per-run latency and memory; network dependency in morning job |
| C. Scheduled download → filtered SQLite import (only allowlisted routes/platforms) | stdlib `sqlite3`, `zipfile`, `csv`, `hashlib` | daily | few MB DB; fast indexed lookup | one new job + table; must handle failed/partial import atomically |

**Recommendation: C, with A's allowlist as the scope filter.**

- Add a new CLI subcommand, `commutecompass gtfs-refresh`, scheduled daily
  before `morning` by a NixOS timer.
- It downloads `gtfs_supplemented.zip`, verifies its sha256, and streams
  `stop_times.txt`. It keeps only allowlisted platforms and their trips: the
  full stop sequence for each matched trip, which the `num_stops` check needs.
- It writes into new tables (`gtfs_feed`, `gtfs_trip`, `gtfs_stop_time`,
  `gtfs_service`) in a separate `gtfs.db` (D2),
  inside one transaction, and then swaps the active `feed_version`.
- No new dependencies are needed.
- **Failure behaviour.** A refresh failure keeps the previous import. The
  bridge then produces nothing for dates outside `feed_start_date..end_date`,
  or after a maximum age of 8 days, which covers the 7-day supplement (D6).
  Refresh runs daily at 04:30 NYC. Planning fails open; adjustment fails closed.
- **Data.** The vendored `stops_subway.csv` stays for informational departures
  only. Bridge stop data comes from the imported `stops.txt`, which has
  coordinates.
- **New surfaces.** The new CLI subcommand requires `SKILL.md`, a script and
  exit codes (`EXIT_TRANSIENT` on download failure).

## 8. Interaction with poll and padding contracts (unchanged)

- `jobs/poll.py` is unchanged. It still calls `realtime_delay` on persisted
  routes and applies only `status == "observed"` increases, through
  `store.increase_realtime_buffer` (same journey, monotone, preserving
  fired/pending/retry/snooze state). It still claims via `claim_ping_entry`.
- Poll never runs the bridge. Identity exists only if morning or a replan
  produced it from a fresh Directions response. That rules out identity
  appearing during refresh.
- `_same_journey` remains blind to identity and provenance. A replan that
  yields the same journey but loses identity (an index outage, say) still
  keeps the larger persisted buffer. It does not gain one.
- A replan to a different journey gets its own identity, buffer and reason.
  Identity is never carried over.
- `planner.plan_event` behaviour: bridge identity enables `observed`, and
  everything else is the same as today (fail-open zero).
- `poll-replan-safety.md` needs one sentence: "Bridge-validated identity is
  produced only by fresh Directions + static GTFS; it is never copied." No
  contract changes.

## 9. Stage 2 (buses) and stage 3 (LIRR) prerequisites

- **Buses.** Directions bus `short_name` and OBA route ids need a per-route
  mapping, including SBS and LTD variants (`M15-SBS`). Prerequisites:
  - stop disambiguation by coordinates plus direction per route variant,
    because opposite-side stops share names;
  - trip ids and `start_date` checked against the MTA bus RT feeds;
  - per-borough static zips;
  - fixtures for loop routes, where `stop_sequence` is required.
- **LIRR.**
  - Branch-to-route mapping, with `trip_short_name` (train number) as the
    primary key once Directions reliably supplies it.
  - Track and platform identity, and the `calendar_dates`-heavy service.
  - Confirm whether the LIRR RT trip_id equals static.
  - Until then, `_route_matches("LIRR")` stays `False`.

## 10. Test plan

Fixtures go in `tests/fixtures/gtfs_bridge/`. They are tiny hand-built GTFS
CSVs, derived from the real 2026-10 feed but reduced to 2–3 trips per case:

- `calendar.txt`/`calendar_dates.txt` with a pick service
  (`L0S8`, an add on 20261031), a base service removed on a holiday, and a
  service ending mid-range.
- `stop_times.txt` with: a ≥24:00 trip; two trips at the same platform and
  second (ambiguity); a trip whose RT suffix is duplicated in another active
  service; and a same-name complex (`635`/`R20`).
- NYCT RT protobufs, generated in tests the way `test_realtime.feed_with`
  does: no `stop_sequence`, no `delay`, `direction_id` 0 on S trips, the
  1001 extension with and without `direction`, `is_assigned` true and false.

Unit cases:

- Line map: exact match only; `6X` is not the same as `6`; agency or vehicle
  mismatch rejects.
- Station gate: right name with far-off coordinates rejects; right coordinates
  with the other complex's route rejects.
- Service day: a 00:30 Saturday departure matches Friday's 24:30 trip, with
  `start_date` = Friday's date. A trip removed by `calendar_dates` gives no
  identity. A date outside coverage gives no identity.
- DST: 2026-11-01 fall-back, with trips at both 01:30 instants
  (25:30 vs 01:30 of the following service day); each matches only its own
  epoch. 2027-03-14 spring-forward, with a 02:30 nominal time. All
  assertions compare UTC instants and inject the clock.
- Ambiguity: each row in §5 produces `unmatched`, and an unrelated nearer
  delayed trip is never used.
- Adapter: derived delay in UTC; disagreement between shape and stop suffix
  rejects; feed `direction_id` is ignored; unassigned trip (per D4);
  cancellation → `cancelled`; skip → `skipped`.
- Provenance:
  - legacy JSON without provenance, and full `gtfs_*` without provenance →
    `unmatched`;
  - a cached route with provenance → stripped/`unmatched`;
  - round-trip through `Store.upsert_plan`/`get_plan` preserves it exactly.
- Import: atomic swap, hash mismatch keeps the old import, filtered row counts.

End-to-end (`tests/test_jobs.py`), production-shaped:

1. Use a Directions JSON shaped like the real Legacy API. That means
   `transit_details` with `line{short_name,name,agencies,vehicle}`,
   `departure_stop{name,location}`, `headsign`, `num_stops`, and
   `departure_time{value,text,time_zone}`, plus walking steps. The fixture
   is the Q from 14 St-Union Sq, northbound.
2. `morning` → plan persisted with identity and provenance.
3. `poll` with an injected NYCT feed showing the same trip running 6 minutes
   late → `increase_realtime_buffer` moves prep and leave earlier, keeps the
   manual offset, and the claimed ping payload reflects the new time.
4. Variants:
   - the same feed where only the opposite-direction trip is late → no change;
   - a cached-route fallback day → no change;
   - an alert replan with the index unavailable → buffer retained, not reduced.

## 11. Implementation breakdown (atomic commits)

1. `feat(models): add GtfsProvenance to TransitLeg`. Model, default `None`,
   serialization round-trip test.
2. `fix(realtime): require bridge provenance for boarding identity`.
   Covers `_same_boarding`, the `from_cache`/`approximate` guard, and test
   updates.
3. `feat(planner): strip GTFS identity from cached and estimated routes`.
4. `feat(gtfs): add static subway GTFS importer and SQLite index`.
   Streaming, filtered, atomic, hash-checked; fixture tests.
5. `feat(cli): add gtfs-refresh subcommand`, plus `SKILL.md`, the script and
   exit codes.
6. `build(nix): schedule gtfs-refresh timer before morning`.
7. `feat(gtfs): add vendored stage-1 subway allowlist`.
8. `feat(gtfs): validate Directions subway legs against static schedule`.
   Line, station, service-day and DST matching; ambiguity tests.
9. `feat(routing): attach bridge identity to first transit leg`.
10. `feat(realtime): derive NYCT boarding delay for validated trips`.
    Direction from the shape, the extension, sequence/delay derivation,
    `is_assigned`.
11. `test(jobs): cover production-shaped Directions through poll`.
12. `docs: document identity bridge contracts`. Covers
    `poll-replan-safety.md`, `AGENTS.md` gotcha, and follow-ups status.

## 12. Decisions (resolved 2026-10-31)

- **D1. Allowlist:** Utica Av (A/C), W 4 St 8 Av level (A/C) and
  47–50 Sts–Rockefeller Ctr (B/D/F/M), both directions. Stop IDs must be
  confirmed during import (§3).
- **D2. Storage:** a separate `gtfs.db`.
- **D3. Time match:** a ±30 s UTC window between the Directions epoch and the
  static time. More than one candidate trip on the route inside the window
  means rejection.
- **D4. Unassigned NYCT trips** (`is_assigned=false`) count as `unmatched`.
- **D5. Feed:** the supplemented feed first. Fall back to the regular
  `gtfs_subway.zip` if the download or validation fails.
- **D6. Freshness:** an 8-day maximum index age; refresh daily at 04:30 NYC.
- **D7. Rollout:** on by default, with no dry-run flag. The bridge fails open
  and transparently. If the index is missing, stale or ambiguous, or a lookup
  errors, the plan is still produced with no identity (so no realtime
  padding), and the reason is logged at INFO/WARNING. Bridge errors must never
  break planning, morning or poll.
