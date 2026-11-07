# Accepted findings — 2026-11-05

Source `bdec0bfa8abe9950bf5473544cae9cab9489412a`. These findings are source-verified
against that revision; explorer lanes did not execute probes or tests. Dynamic baseline
and the N-1 counterfactual are in [baseline.md](baseline.md). Severity: P1 means
material reminder loss/revival, credential exposure, or a supported workflow blocked;
P2 means correctness/robustness; P3 is hygiene. This is not an exhaustive security
guarantee. D-2 is an alias of F-2, so the canonical total is 26.

## A — persistence/jobs (WT01)

### A-1 — failed-send release collides with replacement
**P1 ·** `src/commutecompass/store.py:217-239,737-751,815-834` has a partial unique
index on `(event_id, kind)` for unfired rows. Claim marks A fired before dispatch;
a concurrent scheduler inserts replacement B; unconditional release of A attempts
to restore `fired=0` and raises `IntegrityError`, aborting poll. Preserve retry grace,
cap, current-payload dispatch and heartbeat. Release must leave newer B pending and A
consumed, with an explicit result if callers need it. Test two Store connections and
a mocked send-failure interleaving; assert no exception and exactly one pending row.

### A-2 — successful morning run leaves obsolete reminders
**P1 ·** `jobs/morning.py:133-192` upserts plans, cancels only absent IDs, skips failed
physical plans and schedules future deadlines. A formerly physical event becoming
virtual, passing its deadline, or producing no route can leave its old leave reminder.
Reconcile each kind atomically with the plan: cancel confirmed removed/past/virtual
obsolete kinds while retaining fired history/retry state. Conservative policy: a
transient route/planning failure must retain the old known-good reminder; do not
bulk-delete on `no_route` outage. Tests distinguish virtual conversion, past
deadline, no-route failure, successful empty fetch, and calendar outage.

### A-3 — alert processed and delivered states conflated
**P2 ·** `jobs/poll.py:406-465` and `store.py:963-979` mark an alert seen after
replan exception, unchanged route, or notifier failure; “seen today” treats any row
as announced. A failed/unchanged A can suppress changed B. Separate processed/replan
state from successful announcement, define bounded retry for failures, and test
failure then changed alert notification, retry, and successful daily dedup. Include
compatibility coverage if a migration is needed.

### A-4 — lexical datetime day membership/order
**P2 ·** `store.py:432-448,983-1007` uses lexical ISO comparisons while other paths
use `julianday`. `2026-11-02T03:00+00:00` is Nov 1 in New York but can be excluded
from the logical day; DST fallback folds can sort incorrectly. Compare instants with
deterministic ties. Test UTC/NY bounds and both fallback folds for plans and pings.

## B — calendar/location

### B-1 — malformed timed event silently omitted
**P1 ·** `calendar_client.py:208-250` continues after malformed noncancelled timed
event data and returns a successful partial payload, so morning treats omission as
authoritative and retires reminders. Raise a payload-free `CalendarFetchError` for
malformed noncancelled/non-valid-all-day events; retain intended cancelled and valid
all-day exclusions. Replace the old skip expectation and test mixed-batch failure plus
morning preservation.

### B-2 — nonhex displayed ID cannot be selected
**P2 ·** `format.py:261-268` displays the first eight characters of any ID, while
`selector.py:37-39,105-108,153-156` accepts hex-like prefixes. Support unique
prefixes over the actual ID alphabet, reject ambiguity, and give exact full IDs
precedence even when one is another's prefix. Do not edit format.py; test nonhex,
ambiguous, exact-precedence, and unchanged title fuzzy matching.

### B-3 — malformed pagination metadata truncates fetch
**P2 ·** `calendar_client.py:200-206,246-248` accepts falsey `nextPageToken=0` as
terminal and does not validate truthy nonstrings or repeated tokens. Validate absent/
null terminal policy and present nonempty string tokens; otherwise raise payload-free
error. Test invalid values, valid multipage/terminal behavior and repeated-token bound.

## C — transit/realtime (WT04)

### C-1 — URL-bearing MTA alert drops feed
**P1 ·** `mta.py:165` calls `HasField('translation')` on a repeated field, raising
`ValueError`; feed-level handling at `103-107,79-86` discards accumulated alerts.
Collect URL text directly as `extract_text` does at `197-204`, and test a real protobuf
URL plus multiple entities surviving extraction.

### C-2 — alert system/time checks use different legs
**P2 ·** `mta.py:470-477,497-499,542-554` checks systems/lines separately from
active periods; wildcard membership can cross subway/LIRR legs. Evaluate system,
line/wildcard and time on the same leg; retain empty-route whole-system semantics.
Test negative cross-leg and cross-system wildcard cases plus same-leg positives.

### C-3 — valid end-only active period discarded
**P2 ·** `mta.py:127-137,207-210` rejects missing starts despite `periods_overlap`
open-bound support at `566-584`. Normalize absent start as an aware lower bound, or
use an optional-start model only after ownership expansion; distinguish absent from
malformed and preserve inverted rejection. Test future/expired end-only protobuf
periods and invalid/inverted values.

## D — interface/config/notification

### D-1 — heartbeat URL leaks in config show
**P1 ·** `config.py:270-285,588-615` omits `monitoring.heartbeat_url` from complete
credential-bearing URL redaction. A synthetic URL containing a token/query can appear
in TOML/JSON and be relayed by the skill. Redact the complete field path and test both
renderers with a sentinel.

### D-2 — alias of F-2
Not counted separately. The config directory permission aspect is the platform access
contract in F-2; do not broaden `/etc` permissions.

### D-3 — config replacement loses ownership and concurrent updates
**P2 ·** `config.py:751-762,790-806,827-836` copies mode but not required ownership;
unlocked read-modify-replace and per-key reset lose disjoint updates or leave partial
reset. After F-2's access contract, preserve/fail safely on ownership, lock the full
read-validate-write transaction, and make reset one transaction. Test supported mode/
group, deterministic concurrent writes, and injected reset failure preserving bytes.

### D-4 — mutating plan-event described as pure read
**P2 ·** `skills/commutecompass/SKILL.md:215-217` omits that ordinary `plan-event`
persists plans/pings (`cli.py:529-532,595,636-637`). Correct authorization language;
`--from` remains preview and never saves. Test the skill contract; do not change CLI
in this lane.

### D-5 — Markdown chunker splits entities
**P2 ·** `notify.py:52-84,117-124` splits at hard/newline boundaries and formats chunks
independently, allowing a bold/link entity to cross a boundary. Implement a bounded
entity-preserving or safe fallback; preserve size, partial-send and stdout/HA behavior.
Test formatting across a boundary.

### D-6 — literal backslash is not escaped
**P2 ·** `format.py:114-123` omits backslash from `_SPECIAL`, and the test currently
codifies unchanged output. Escape original backslashes safely before punctuation and
test literal/trailing/adjacent cases plus HA unescape behavior. Combined WT07.

### D-7 — Click usage exits 2, not contract 64
**P2 ·** `cli.py:27-34,95-104` declares usage 64 but Click handles unknown options/
missing arguments first. Normalize top-level usage exceptions to 64; preserve help 0
and app 65/66/75/78. Test CliRunner and public entry as needed; no SKILL change.

### D-8 — config edit parse errors are unnormalized
**P2 ·** `config.py:790-791,827-828` and `cli.py:1056-1127` do not normalize malformed
TOML/UTF-8 during set/unset/reset. Return safe 78 diagnostics without parser text,
leave bytes unchanged, and test all three commands with a sentinel.

## E — alarm/HA (WT08)

[Detailed implementation contract and evidence limits](design-decisions.md).

### E-1 — manual intent survives lost retirement
**P1 ·** `alarm_daemon.py:1441-1452,1626-1636` saves the terminal transition before
retirement, which requires a fresh ended-wake delta. A crash or retirement exception
can leave manual intent live after dismissal and re-advertised by later publication.
Persisted done suppresses immediate same-instant replay by this daemon with an intact
cache; do not claim that replay is inevitable. Make retirement durable and idempotent
with the original manual version; test restart and replay after an identical replacement
has itself been rebound. See the [design acceptance matrix](design-decisions.md).

### E-2 — HA acceptance precedes durable manual binding
**P1 ·** `alarm.py:691-699,724-740` posts to HA, then saves history and binds
separately with suppressed errors. A crash or DB error leaves accepted but unbound
intent that HA/daemon retirement cannot clear (successful CLI skip directly clears
intent and is not this failure). Use recoverable pending publication/binding
identity and version isolation; do not pretend reordering solves the distributed
boundary. Inject failures after acceptance and restart/skip/dismiss.

### E-3 — tomorrow failure bypasses alarm recovery
**P2 ·** `jobs/tomorrow.py:71-76` returns on `plan_window=None` before safe publish,
unlike recovery paths at `alarm.py:375-405`. Distinguish explicit fetch failure from
successful empty results, invoke bounded recovery and warning, and preserve disabled/
legacy behavior. Test tomorrow plus recovery.

### E-4 — HA HTTP exception text leaks tokens
**P2 ·** `alarm_daemon.py:131-132,237-239` and `alarm.py:555-556` log arbitrary
HTTP exception text; synthetic `ConnectError('FAKE_TOKEN')` reaches logs. Emit only
operation and exception class or established redaction; test cached/failure behavior
without the sentinel.

### N-1 — dry-run scratch path is agent-specific
**P1 ·** `alarm.py:657-661,680` uses `TemporaryDirectory(...,
 dir='/tmp/opencode')` and broad fallback. The baseline Nix test fails; the
counterfactual probe shows missing parent causes the same failure and removing the
explicit directory passes, but this is not a patch/full-suite validation. Use normal
`tempfile` defaults/TMPDIR, 0700 cleanup, and preserve original DB/token on dry run.
Report setup exception class only, never arbitrary exception text. This is the first
small WT08 substep.

## F — platform

### F-1 — fork CI unconditionally authenticates cache
**P1 ·** `.github/workflows/ci.yml:41-55` and `.github/actions/setup-nix-attic/action.yml:4-7,22-26`
login even without `ATTIC_KEY`; make auth conditional on a nonempty credential while
fork checks/install/build still run and no write secret reaches fork code.

### F-2 — distinct skill user lacks state/config access
**P1 ·** `nix/module.nix:50-51,303-325,375-380` promises interactive skill users,
but state/config modes, umask and SQLite sidecars prevent DB/config mutation. Define
least-privilege service/skill/unrelated-user access, including WAL/SHM, activation,
restart and redeploy persistence. The chosen [service-UID broker and persistent
allowlisted overlay](design-decisions.md) adds implementation and migration scope;
it is a selected design, not another observed defect. Validate in a synthetic NixOS
VM; module-eval alone is insufficient.

### F-3 — documented optional secret target is mandatory
**P2 ·** `nix/module.nix:195-205` requires a string while docs say omit and use an
environment source; generated units interpolate it. Make null the default and omit
`OPENCLAW_TARGET` when absent; preserve configured public values and env-file secrets.
Test absent/present unit output without synthetic secrets.

### F-4 — generated coverage artifact tracked
**P3 ·** `.coverage` is a tracked 217088-byte generated artifact and no ignore rule
exists. Remove the tracked file and ignore `.coverage`/`.coverage.*`; do not rewrite
history or clean worktrees. Verify tracked lookup fails, ignore lookup succeeds, and
a coverage run does not mutate tracked files.

## Residual and future documentation follow-ups

Previous audit fixes remain implemented as described in `../implementation-status.md`.
Unproven residual risks include malformed GTFS zip/CSV and edge matching, live provider
behavior, resolver timeout/retry behavior, and not every CLI/formatter/HTTP path.
SQLite `ResourceWarning` from the baseline has no assigned fix because its cause was
not investigated. The stale “not implemented” label in
`../../realtime-identity-bridge.md` versus implemented stage 1 needs a future
documentation owner. The GTFS allowlist path drift and missing architecture map in
`AGENTS.md` require a separately authorized agent-policy owner; this documenter may
not edit them. No live deployment, credentials, private origin, or HA/provider call
was used.
