# Detailed design contracts — 2026-11-05

[Audit index](README.md) · [Fixer lanes](lanes.md) · source
`bdec0bfa8abe9950bf5473544cae9cab9489412a`.

These are **chosen implementation designs**, not additional observed defects or
validated implementations. Following an explicit complexity check, the user chose
**Keep detailed designs**. The broker/overlay and alarm bookkeeping add scope and
migration risk; do not disguise them as trivial patches. The review was read-only:
no runtime databases, credentials, live APIs, tests, builds or Nix evaluation.
The smaller fixes in other lanes do not depend on proving these larger designs.

## Alarm: bounded write-ahead association and version-pinned retirement

Use alarm-specific persistence, not a generic workflow engine, distributed transaction,
or new HA wire protocol. E-1/E-2 remain one owner because publication and retirement
must share the same immutable intent identity.

### Corrected evidence boundaries

- **E-1:** `alarm_daemon.py:1441-1452,1626-1636` saves the terminal transition before
  retirement and requires a fresh `ended_wake` delta. A crash or retirement exception
  loses that opportunity. The defect is **manual intent remaining live after dismissal
  and being re-advertised by later publication**. With an intact cache, persisted
  `done` suppresses immediate same-instant replay by this daemon. Such replay is not
  demonstrated and must not be claimed as inevitable.
- **E-2:** `alarm.py:691-699,724-740` performs separate history/binding writes after HA
  publication. A local transaction alone cannot close acceptance-before-commit.
  HA/daemon retirement depends on binding; successful CLI `alarm skip` directly
  clears intent (`cli.py:467-475`) and is not the broken skip path.
- **E-3:** `tests/test_jobs_tomorrow.py:454-466` explicitly expects no publication on
  calendar failure. Change only the wake-alarm safety expectation, not the no-legacy-
  prep-push behavior. Failed day is not a verified empty day.
- **E-4:** arbitrary HTTP exception text is unsafe to disclose. A synthetic token can
  reach it; this does not mean every ordinary HTTP exception contains a bearer token.

### Required invariants

1. Before a manual-linked HA send, persist an immutable association of the observed
   `manual_version`, manual deadline, selected wake/deadline and publication attempt.
   Identical values in a replacement are a different intent. Preparation neither
   consumes intent nor marks success. If preparation fails, return the existing
   transient failure surface without sending an untrackable manual-linked wake.
   Keep SQL transactions short and never hold one across HTTP.
2. Confirmed publication updates successful history and association in **one Store
   transaction**. V1 still requires exact readback and stable attempt identity.
   Legacy HTTP errors may leave remote acceptance unknown: do not promote unconfirmed
   attempts to last-success history or automatically replay them. Keep their immutable
   association while relevant so later observed termination can retire the original
   version even after a lost response. Never rebind retained history by deadline alone.
3. Pin the original manual version to the daemon's pending/active session. Persist
   pending retirement alongside the terminal transition, separately from the latest
   display-only `ended_wake`. Retry after restart/bookkeeping failure and acknowledge
   only after an idempotent Store result. SQL must compare the **expected original
   version**, not just current binding/current version equality. Do not overwrite an
   unresolved retirement when another terminal arrives.
4. Before selecting manual intent, the publisher reconciles durable pending retirements
   using the same idempotent SQL operation. It does **not** write/acknowledge the daemon
   cache; the daemon remains that cache's sole writer. An old retirement cannot clear
   a replacement because the expected version is immutable.
5. Desktop dismissal and observed explicit skip/clear retire only their associated
   intent. Outage, schedule replacement, recovery expiry and timeout do not newly
   authorize consuming intent. A **currently observed** master-off remains global;
   never replay an old master-off terminal as a fresh global clear. Preserve
   state-before-playback and guaranteed audio stop/restore despite retirement errors.
6. Legacy time-based identity can be ambiguous. Already pinned sessions retain their
   version. Old cache/history without version evidence must not be attached to the
   current replacement by matching times. Report unresolved retirement, preserve the
   replacement and permit normal expiry; do not guess a destructive migration.

This covers **process-crash bookkeeping recovery**, not HA/SQLite atomicity,
remote rollback, corrupt/lost storage, or machine-power-loss durability. SQLite's
existing `synchronous=NORMAL` is not a stronger guarantee.

### Acceptance matrix

| Scenario/injection | Required result |
|---|---|
| Crash after preparation, before HA | Intent retained, no success history, no automatic journal replay |
| HA accepts, then process dies or success persistence fails | Original association survives; matching later retirement can consume only that version |
| Terminal cache save then crash before retirement | Restart or subsequent publisher reconciles without a new terminal delta |
| Retirement failure; crash after SQL clear before cache acknowledgement | Retry idempotent, audio restores |
| Replace intent during publication, including identical values | Old attempt cannot bind/clear replacement |
| Rebind identical replacement, then replay old terminal | Replacement survives; essential regression |
| Earlier calendar selection, logical-day boundary, DST fold | UTC identity still retires correct original version |
| HA outage/replaced schedule; live versus stale master-off | Preserve the control distinctions above |
| Legacy cache/history lacks version or is ambiguous | No guessed deletion/rebinding |
| Tomorrow planning returns `None` | No legacy prep push; enabled non-dry-run alarm uses retained/manual/fallback safety with explicit failure context |
| Synthetic secret in HTTP/chained exception | Absent from logs/rendered errors; operation plus exception class only |
| Dry-run without agent scratch directory | Normal tempfile/TMPDIR, private scratch, cleanup, original DB/token unchanged |

Pass failed-day context explicitly into alarm safety; never turn `None` into `[]`
or introduce a second unbounded calendar fetch. Preserve successful precomputed plans,
current-day selection, disabled behavior and dry-run isolation. N-1 is the first small
portability patch under the same owner; do not redesign SQLite snapshots opportunistically.
Separate logical commits only when commits are explicitly authorized.

## Deployment: one service UID, restricted broker and persistent allowlisted overlay

### Why this contract

Mutation is supported by `skill.users` (`nix/module.nix:303-326`), README and skill
commands. It is not intentionally read-only. Existing wrappers run as the caller,
while service creation is owner-only (`module.nix:50-51,79-80,355-380`), and atomic
config replacement needs directory write access (`config.py:751-762`).

Keep one service UID responsible for SQLite/application files and retain private
state plus restrictive umask. Do not solve this by group-writable state: WAL/SHM,
locks, OAuth caches and replacement ownership widen that trust contract unnecessarily.

### Broker boundary

- `commutecompass-skill` calls a fixed administrator-owned broker **as `cfg.user`,
  never root**, through a narrowly scoped run-as rule. No arbitrary sudo, shell,
  systemctl or caller-selected executable.
- Validate the documented skill command/option grammar. Reject arbitrary global
  options and caller-selected config/override paths. Preserve argv; no shell
  interpolation. A fixed executable is not safe if it forwards unrestricted CLI
  arguments.
- Start with a controlled environment. Do not inherit caller `PYTHONPATH`, dynamic
  loader variables, credential variables or configuration-path variables.
- After changing identity, load only the fixed administrator-controlled credential
  file. No caller-selected source file. The service identity, **not skill users**,
  must read it: service-owned `0400` or suitable root/service-group `0440`. This fix
  must not chmod or manage `/run/secrets` payloads.
- Remove the need for skill users to join a credential-bearing service group.
  Document operational authority honestly; the broker does not make arbitrary CLI
  functions safe for untrusted users. Reusing a login account as `cfg.user` needs
  explicit trust review.

### Configuration semantics frozen for WT05

| Layer/operation | Contract |
|---|---|
| Base | Immutable administrator-owned `/etc/commutecompass/config.toml`, service-readable |
| Overlay | `${dataDir}/config-overrides.toml`, service-owned, initially absent/empty and retained across activation |
| Precedence | Schema defaults → Nix base → allowlisted overlay → environment-only credentials |
| Overlay grammar | Only `CONFIG_SET_ALLOWLIST`; never secrets, origin, paths, calendars or arbitrary tables |
| Managed set | Set overlay value |
| Managed unset | Remove overlay value; expose deployed base or schema default if base omits it |
| Managed reset | Remove all overrides in one transaction, base unchanged |
| Standalone single-file mode | Preserve existing schema-default-on-delete behavior |

All service and broker invocations use the same fixed overlay target. Managed
unset/reset inheritance is an intentional documented contract change; no default/null
tombstones. Use a **stable sidecar lock** around the complete read–edit–validate–replace
transaction, not the replaceable target inode. Validate the effective merged config
for set/unset/reset; reset performs one replacement. Preserve required uid/gid/mode
before replacement or fail without changing original bytes. Malformed/permission
failures use safe diagnostics. Activation never overwrites overrides. A newly
incompatible base fails clearly rather than silently dropping overrides.

### Migration and deployment acceptance

- Quiesce existing writers before ownership repair/migration; account for existing DB,
  WAL/SHM and lock files. Never delete live sidecars or recursively loosen permissions.
- Existing mutable `/etc` edits need **operator-reviewed allowlisted differences**,
  not automatic wholesale import. New installations must not require a migration.
- F-3 separately makes the HA target optional and omits the generated environment
  variable when absent; public configured values remain supported. No secret enters
  the Nix store.
- A synthetic NixOS runtime VM must use distinct service/skill users and verify DB
  reads/mutations with WAL, existing files, config mutation/concurrent edits,
  restart and repeated activation persistence, and denial to an unauthorized user.
  Reject caller path/env/argument injection; prove no secret readable by skill users.
  Module evaluation alone cannot establish these properties. No live rollout is part
  of this audit.

## Sequential ownership and integration

- **WT01 → WT02 → WT08.** Transfer `store.py` and `tests/test_store.py` exclusively
  to WT08 after prerequisites integrate. WT08 uses Store transactions/version-pinned
  compare-clear, never duplicate ad-hoc SQL in `alarm_state.py`. WT08 also owns
  `docs/alarm-design.md` for its recovery contract.
- **WT06 → WT10 → WT05.** WT10 freezes broker/base/overlay semantics, owns module,
  wrapper, broker helper/tests and deployment README, then transfers config/CLI
  implementation requirements to WT05. WT10 receives `SKILL.md` only after WT06
  integrates. Do not create a parallel SKILL writer or leave reset/trust wording stale.
- WT10's final module/VM integration follows WT05. If module wiring needs the not-yet-
  present overlay interface, mark it unvalidated until WT05 lands; do not claim an
  intermediate deployable state. Final runtime gate follows both integrated lanes.
- Future orchestrator obtains the worktree/Git approvals described in [lanes](lanes.md).
  No source implementation, migration or validation of these designs occurred here.
