# WT-G — Secret-bearing HTTP logs and Telegram acknowledgement

**Base:** `4df247f`; follow [shared execution rules](README.md#execution-rules).
**Slug / branch:** `audit-sensitive-http` / `fix/audit-sensitive-http`.
**Exclusive write scope:** `src/commutecompass/monitoring.py`, `src/commutecompass/notify.py`,
`tests/test_monitoring.py`, `tests/test_notify.py`; if needed, add
`src/commutecompass/http_logging.py` and `tests/test_http_logging.py`.
**Validation owner:** this lane's fixer; integration owner runs the final suite.

## MONITORING-1 — P1 — heartbeat URL token appears in logs

**Source:** `monitoring.py:37-38` logs the entire URL and exception. Healthcheck URLs
are bearer-like credentials. **Reproduced offline:** mocked HTTP 403 with a fake
sentinel in the URL returns `False` but exposes the sentinel through the monitoring
and httpx loggers. Successful requests can also expose URLs in httpx request logs.

**Correction to initial audit:** `retry.py:72-78` already logs only exception class
names. It is not a confirmed leak and is outside this lane's write scope. Keep its
safe behavior; use it as a read-only regression boundary.

## NOTIFY-1 — P1 — bot token URL appears in logs

**Source:** `notify.py:112` puts the token in the URL; `:125-126` logs raw HTTP error
text. **Reproduced offline:** a mocked `RequestError` containing a fake bot-token URL
returns `False` and exposes the token through application logs. Normal transport
request logs can expose it too. `:129-134` also emits raw response bodies; removing
that content is privacy hardening, not proof that Telegram actually echoes secrets.

### Shared logging fix boundary

Use safe structured diagnostics (operation, status, exception class), not raw URLs,
exception strings or provider bodies. Protect relevant httpx/httpcore log output too,
including success and retry paths. Choose a narrowly scoped redaction/transport-log
policy that does not globally disable unrelated application logs or mutate root
logger levels around requests (unsafe under concurrent jobs). If a shared helper is
needed, only this lane owns the new `http_logging.py`. Do not broadly refactor HTTP
clients or edit retry/poll/config. Keep useful failure visibility without chat IDs,
message bodies, token URLs, or heartbeat credentials.

**Logging acceptance/tests:** capture the application, retry, httpx and httpcore
loggers at DEBUG with synthetic sentinels. Exercise success, 4xx, transient retry,
and transport exceptions; sentinel URL path/query/token values never appear in
formatted records or retained record arguments/exception info. Test two concurrent
fake clients with different sentinels to catch unsafe global logger mutations.
Heartbeat still returns `True` on success and `False` on failure; notifier chunking,
stdout framing and existing send return values remain compatible except below.

## NOTIFY-3 — P2 — HTTP 200 is accepted without a valid Telegram acknowledgement

**Source/reproduction:** `notify.py:137` returns `True` solely from HTTP status.
Mocked HTTP 200 bodies `{"ok": false}` and `[]` both return `True` today.
No real Telegram HTTP-200/ok-false incident was established; this is robustness
against malformed or intermediary responses, not a proven provider outage mode.

Require a JSON mapping with `ok is True` for each chunk. Reject malformed JSON,
wrong-shaped JSON, missing/false/wrong-typed `ok`, and non-200 responses with a safe
failure diagnostic. Stop after the first failed chunk; retain existing bounded
message splitting. Do not add transport retries here: retrying a successfully sent
but unacknowledged chunk can duplicate delivery. Preserve the poll's existing
claim/release/retry policy without editing it.

**Acknowledgement acceptance/tests:** `ok:true` succeeds; `ok:false`, absent/string
`ok`, malformed JSON and list/scalar JSON fail; later-chunk failure returns `False`
and stops further chunks. No response body/message text leaks into logs. Update
existing fake success responses to reflect the actual Telegram acknowledgement.

**Focused command:** `pytest -q tests/test_monitoring.py tests/test_notify.py tests/test_retry.py`
(and `tests/test_http_logging.py` if created).
**Commits:** keep logging fixes separate from acknowledgement behavior, e.g.
`fix(logging): redact secret-bearing http diagnostics` and
`fix(notify): require telegram success acknowledgement`.
