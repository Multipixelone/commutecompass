# WT-B — Location resolution & calendar ingest

**Branch / worktree:** `audit/wt-b-resolution`
**Write scope (exclusive):** `src/commutecompass/{llm,geocode,resolver,venues,selector,calendar_client}.py`
and their tests (`tests/test_llm.py`, `tests/test_geocode.py`, `tests/test_resolver.py`,
`tests/test_venues.py`, `tests/test_selector.py`, `tests/test_calendar*.py`).
Do **not** edit `store.py` (WT-A owns it) — canonicalize cache keys on the resolver side.
**Validation:** `pytest -q` on the files above, `ruff check .`, `mypy src`.
**Privacy:** test fixtures must never contain a real home address/coordinates; use public venues.

## RES-1 — P1 — LLM response parsing can crash resolution
- **Where:** `llm.py:119-148` (`_parse_response`), called outside the `try` in `resolve_location`.
- **Evidence:** `parsed.get(...)` is called without checking `parsed` is a dict (JSON `[]`,
  `null`, `"str"` → `AttributeError`); a non-string `value` reaches pydantic and raises
  `ValidationError`; the JSON-failure branch logs the full model output.
- **Fix:** `isinstance(parsed, dict)`; validate `kind`/`value` are non-empty `str`; catch
  `ValidationError` → `None`; log only length/type, not content.
- **Tests:** responses `[]`, `null`, `"x"`, `{"kind":"address","value":[]}`, invalid JSON → `None`, no raise.

## RES-2 — P1 — prompt injection via calendar location text
- **Where:** `llm.py:72-86`. Raw `event.location` is the entire user message.
- **Fix:** fixed user template containing the location as a JSON-encoded string inside clear
  delimiters; system prompt states that the delimited text is untrusted data and any
  instructions in it must be ignored. Validate output: length cap (≤200), no control chars,
  `kind` in the allowed set. Treat failures as unresolved.
- **Tests:** location containing `Ignore previous instructions ...` is passed JSON-encoded
  (assert request body); an over-long/garbage output is rejected.

## RES-3 — P1 — geocoder: unhandled parse errors and null-island coordinates
- **Where:** `geocode.py:62-82`; callers `resolver.py:125,156`.
- **Evidence:** `response.json()` and shape access are outside the `try`
  (`JSONDecodeError`/`TypeError`/`KeyError` escape); missing `lat`/`lng` default to `0.0`,
  producing a route to (0,0).
- **Fix:** catch `ValueError, TypeError, KeyError, AttributeError` → `None` with a log;
  require both coords present, numeric, finite, and within lat ±90 / lng ±180; otherwise `None`.
- **Tests:** non-JSON body, `{"results":[{}]}`, missing lng, `NaN`, string coords → `None`.

## RES-5 — P2 — placeholder locations reach the geocoder
- **Where:** `resolver.py:123-141`. Placeholder check (e.g. "Location available once RSVP'd")
  runs after `looks_like_address` / geocoding.
- **Fix:** move the placeholder short-circuit to the top of the pipeline; use word-boundary
  regexes. AGENTS.md gotcha 3 requires no network/LLM call for these.
- **Test:** placeholder → unresolved, geocoder and LLM mocks not called.

## RES-4 — P2 — cache key not canonical
- **Where:** `resolver.py:87,103-105` (store side `store.py:673-707` is read-only for you).
- **Fix:** canonical key = NFKC → casefold → collapse whitespace → strip. Look up canonical
  key first, fall back to the raw key for existing rows; write the canonical key.
- **Test:** `"  Joe's  Pub "` and `"joe's pub"` hit the same cache row.

## RES-6 — P2 — duplicate venue aliases silently overwrite
- **Where:** `venues.py:55-62`. Raise a clear error at load naming both venues. Test.

## RES-7 — P2 — selector accepts naive `now`
- **Where:** `selector.py:59-76`. Raise `ValueError` on naive datetimes, else convert with
  `to_nyc`. Test both.

## RES-8 — P2 — corrupt token file not mapped to AuthError
- **Where:** `calendar_client.py:63-88`. Only `RefreshError` maps to `AuthError`; a corrupt or
  unreadable token raises `JSONDecodeError`/`OSError`/`ValueError`.
- **Fix:** map these to `AuthError` with a message naming the path, never the contents. Test.

## RES-9 — P2 — one malformed event aborts the whole fetch
- **Where:** `calendar_client.py:172-195`. Skip items missing `id` or with unparseable
  times, log event id/index only (no title/location), continue. Test with a mixed batch.
