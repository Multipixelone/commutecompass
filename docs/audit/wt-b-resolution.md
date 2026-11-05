# WT-B — Calendar ingestion and OAuth persistence

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-calendar` / `fix/audit-calendar`.
**Exclusive write scope:** `src/commutecompass/calendar_client.py`, `tests/test_calendar_client.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## RES-10 — P2 — malformed items container escapes ingestion

**Confirmed by source:** `CalendarClient.fetch_events`, `calendar_client.py:159-218`,
gets `items` and iterates it outside the per-item exception handler. `items=None`
or an integer raises `TypeError`; a dict/string is silently iterated as keys/chars.
The existing per-event handler does not validate the container itself.

Validate the response mapping and `items` list shape before iteration. A malformed
page must not masquerade as a successful, authoritative empty calendar: raise a
controlled fetch error so morning's failed-fetch path can preserve reminders. A
missing `items` key on a normal empty response may remain a successful empty list.
Keep valid-event per-item isolation, enabled-calendar selection, and timezone rules.

**Acceptance/tests:** mock the Calendar service chain, without authorization/network;
exercise `items=None`, scalar, string, mapping, absent key, `[]`, and mixed valid/invalid
events. Invalid containers produce the chosen controlled error, not an accidental
iteration exception; valid records retain NYC-aware times. Do not expand pagination
or change multi-calendar completeness semantics without a separate design decision.

## RES-13 — P2 — credential writes are non-atomic and briefly over-permissive

**Confirmed by source:** `_save_credentials`, `calendar_client.py:98-103`, uses
`Path.write_text` before `chmod(0600)`. New files inherit umask before chmod, and an
interrupted overwrite can destroy a previously valid refresh token.

Use a same-directory temporary file created with mode 0600, write/flush/fsync, then
`os.replace`. Clean up temporary files on failure. Do not repurpose the config-file
writer: its permission-preservation semantics differ, and another lane owns config.
Keep credentials out of exceptions/logs and fixtures; test with fake serialized data.

**Acceptance/tests:** new/replaced token mode is 0600 under permissive umask; simulated
write/fsync/replace failures preserve the old bytes and leave no temp files; successful
replacement contains the fake new bytes. Never open a real credential file.

**Focused command:** `pytest -q tests/test_calendar_client.py`.
**Commits:** one per finding, e.g. `fix(calendar): reject malformed event containers`
and `fix(calendar): persist oauth credentials atomically`.

## Legacy audit disposition

RES-1 through RES-9 are resolved in current LLM/resolver/geocode/venues/selector/calendar
code and tests. Do not rework them. Re-audit claims RES-11 and RES-12 were withdrawn
on source review. Unbounded raw LLM input is an **unconfirmed P3 investigation**, not
an implementation order; define a concrete failure and compatibility-safe limit first.
