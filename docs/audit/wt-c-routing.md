# WT-C — Routing, weather, MTA alerts, GTFS

**Branch / worktree:** `audit/wt-c-routing`
**Write scope (exclusive):** `src/commutecompass/{routing,weather,mta,gtfs_index}.py` and their
tests (`tests/test_routing*.py`, `tests/test_weather.py`, `tests/test_mta*.py`, `tests/test_gtfs*.py`).
**Invariant (AGENTS.md gotcha 8):** only `gtfs_bridge.validate_leg` sets `gtfs_*` identity; do not
touch `gtfs_bridge.py`. Bridge/refresh failures must fail open.
**Validation:** `pytest -q` on the files above, `ruff check .`, `mypy src`.

## RTE-2 — P1 — plan_route lets parse errors escape
- **Where:** `routing.py:515-532`. Only `httpx.HTTPError` is caught; `response.json()`
  `ValueError` and any `_parse_route` error propagate into planning/morning/poll.
- **Fix:** catch `ValueError, TypeError, KeyError, AttributeError` around decode+parse → log
  (type only) and return `None`.
- **Test:** non-JSON body and `{"routes": [None]}` → `None`, no raise.

## RTE-3 — P1 — _parse_route assumes dict shapes
- **Where:** `routing.py:385-405`. Skip non-dict routes/legs/steps instead of raising
  `AttributeError`; if no valid alternative remains, return `None`.
- **Test:** mixed list with one malformed and one valid route → valid one chosen.

## RTE-1 — P1 — unvalidated non-transit step timestamps
- **Where:** `routing.py:240-255`. Walking/other step departure/arrival values go straight to
  `datetime.fromtimestamp` (str/NaN/huge → `TypeError`/`ValueError`/`OverflowError`/`OSError`),
  and the fallback uses `datetime.now` (wall clock in the middle of a parse).
- **Fix:** reuse the existing validated `_scheduled_time()` helper; derive fallback from the
  neighbouring leg times / request time, not `now()`. Results must be tz-aware NYC.
- **Test:** step timestamps `"abc"`, `float("nan")`, `1e20` → no raise, sensible times.

## RTE-4 — P2 — weather forecast ambiguous across DST fall-back
- **Where:** `weather.py:87-92`. Matches hourly forecast by naive local-hour string with
  `.index()`; on 2026-11-01 the 01:00 hour occurs twice and the first is always chosen.
- **Fix:** request UTC/unix times (Open-Meteo `timeformat=unixtime` or `timezone=GMT`) and match
  by instant.
- **Test:** fixture covering 2026-11-01 05:00Z and 06:00Z (both 01:00 local) → distinct entries.

## RTE-5 — P2 — inverted alert active periods accepted
- **Where:** `mta.py:125-136, 537-548`. Drop periods with `end < start` (log). Test.

## RTE-6 — P2 — negative alert timestamps accepted
- **Where:** `mta.py:200-208` `_parse_timestamp` rejects only `0`. Use `seconds <= 0`. Test `-1`.

## RTE-7 — P2 — GTFS refresh fallback too narrow
- **Where:** `gtfs_index.py:713-727`. Supplemented-feed failure only falls back on
  `AllowlistMismatchError/GtfsImportError/OSError/sqlite3.Error`; `ValueError`, `BadZipFile`,
  `KeyError`, `UnicodeError` abort before the regular-feed attempt.
- **Fix:** per-feed-attempt `except Exception` (log exception type + feed name), then try the
  regular feed; the final failure still exits non-zero and leaves the previous `gtfs.db` intact.
- **Test:** supplemented import raises `BadZipFile` → regular feed is used.

## RTE-8 — P2 — download() leaks OSError and partial files
- **Where:** `gtfs_index.py:673-688`. Wrap dest open/write `OSError` as `FeedError`, write to a
  temp file and `os.replace` on success, unlink the partial on failure. Test with an unwritable dest.
