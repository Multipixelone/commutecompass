# WT-C — Route endpoint and weather parsing

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-provider-parsing` / `fix/audit-provider-parsing`.
**Exclusive write scope:** `src/commutecompass/routing.py`, `src/commutecompass/weather.py`,
`tests/test_routing.py`, `tests/test_weather.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## RTE-NEW1 — P2 — overall endpoint timestamps bypass validation

**Source:** `routing.py:454-467` converts provider departure/arrival values directly
with `datetime.fromtimestamp`; missing/zero values fall back to wall-clock now.
`plan_route` at `:547-562` catches several parse exceptions, but not `OverflowError`
or `OSError`. **Reproduced offline:** an otherwise usable mocked Directions response
with overall departure `1e100` raises `OverflowError` through `plan_route`.
Infinity was contained as `ValueError` on the audit platform; do not claim every
invalid timestamp escapes. Severity is P2 because this needs malformed provider data.

Harden the existing timestamp validator itself: `_scheduled_time` (`:207-219`)
calls `math.isfinite` before its guarded conversion, so `10**1000` also raises there,
including through `route_timing`. Use the hardened validator for overall endpoints.
Preserve otherwise-usable duration-only routes and normally untimed non-transit
responses, including existing zero/negative endpoint behavior (`tests/test_routing.py:163-190`).
Use chronological validated endpoints from the selected candidate. With one valid
endpoint and usable duration, derive only the missing display endpoint using guarded
UTC-instant arithmetic. With neither valid, anchor display arrival to explicit request
arrival time; production `plan_route` must pass it. Direct-parser calls without either
endpoint or request time use a fixed aware epoch display placeholder, preserving
usable duration-only compatibility. Guard duration and arithmetic overflow too.
Never borrow nested transit-leg wall-clock placeholders (`routing.py:250-273`).
In `route_timing`, missing/invalid provider timestamps must remain unavailable; do
not promote derived display timestamps into provider schedules. Reject genuinely
unusable routes, not every route missing an endpoint. Defensive outer exception
handling does not replace numeric validation. Preserve live/legacy/cached timing,
selected alternatives, routing modes, and bridge-only GTFS identity provenance.

**Acceptance/tests:** parameterize departure and arrival independently with strings,
bools, NaN, infinity, `1e100`, huge integers, zero, negative, and absent values. No
uncaught exception from the public planner; each case gives a documented safe fallback
or no-route result without discarding usable duration-only routes. Valid endpoints
remain NYC-aware, and fallbacks are independent of wall-clock time. Include missing
nested transit timestamps and direct helper/route_timing huge-integer tests; keep
existing valid/multi-leg, live/legacy/cache and selected-alternative coverage.

## WX-NEW1 — P2 — numeric weather parsing escapes fail-open boundary

**Source:** `weather.py:47-54` protects fetching but calls `_buffer_from_hourly`
outside the try. `_at` at `:111-118` converts numeric series entries with `float`.
**Reproduced offline:** a response-shaped hourly dict with a matching epoch hour and
`precipitation=[10**1000]` raises `OverflowError`. This is a JSON-compatible arbitrary-
precision integer trigger, not an unrealistic replacement of the fetcher's return type.

Contain malformed numeric parsing and reject non-finite values. The documented
clear-weather fallback is `WeatherBuffer(0, None)`; preserve real thresholds and
UTC-epoch hour matching across NYC DST. Prefer a narrow conversion fix plus an
explicit public-boundary contract over unrelated weather refactoring.

**Acceptance/tests:** huge integers, non-finite values, missing/short series, and
ordinary malformed values cannot crash `weather_buffer`; invalid data alone produces
the clear-weather fallback. Valid precipitation/probability/snow and repeated-DST-hour tests
still pass. Use only mocked forecast data.

**Focused command:** `pytest -q tests/test_routing.py tests/test_weather.py`.
**Commits:** separate routing and weather fixes, e.g.
`fix(routing): validate overall route endpoint timestamps` and
`fix(weather): contain malformed numeric forecasts`.

## Legacy audit disposition

RTE-1 through RTE-8 are resolved **as originally stated**: non-transit step timestamps
use `_scheduled_time` (`routing.py:246-255`); normal response parse failures and bad
route/leg/step shapes are contained (`:399-433`, `:547-562`); weather uses epoch-hour
matching (`weather.py:87-90`); inverted/non-positive MTA alert times are rejected
(`mta.py:130-134`, `:207-215`, `:549-553`); supplemented-feed refresh falls back, and
GTFS downloads use bounded streaming/partial-file atomic replacement
(`gtfs_index.py:673-699`, `:724-739`). RTE-NEW1 is the remaining **overall endpoint**
case, not a reason to undo those fixes.
