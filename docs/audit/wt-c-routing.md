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

Use the existing safe timestamp validator for overall endpoints as well as steps.
Choose a deterministic fallback from validated route legs/request time, or reject
the route through the existing no-route path. Avoid fabricating current-time endpoints.
Defensive outer exception handling is useful but does not replace input validation.
Preserve valid schedules, routing modes, and bridge-only GTFS identity provenance.

**Acceptance/tests:** parameterize departure and arrival independently with strings,
bools, NaN, infinity, `1e100`, huge integers, zero, negative, and absent values. No
uncaught exception from the public planner; each case gives a documented safe fallback
or no-route result. Valid endpoints remain NYC-aware, and fallbacks are independent
of wall-clock time. Keep existing valid/multi-leg parsing coverage.

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
the clear-weather fallback. Valid precipitation/wind/snow and repeated-DST-hour tests
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
