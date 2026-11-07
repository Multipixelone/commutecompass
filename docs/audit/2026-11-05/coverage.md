# Coverage manifest — 2026-11-05

The audit assigned all **165 tracked paths** at `bdec0bf` exactly once; the
machine-readable category assignment is [source-paths.tsv](source-paths.tsv).
This is an assignment and review-scope record, not a claim that every path received
equal depth or that the audit is an exhaustive security review.

| Category | Assigned paths | Findings | Lane(s) |
|---|---:|---:|---|
| A | 22 | 4 | WT01 |
| B | 14 | 3 | WT02, WT03 |
| C | 26 | 3 | WT04 |
| D | 48 | 7 (D-2 is F-2 alias) | WT05–WT07 |
| E | 23 | 4 | WT08 |
| F | 32 | 4 | WT09–WT11 |
| **Total** | **165** | **26 canonical** | **WT01–WT11** |

The manifest includes 51 test paths and 28 skill scripts. No tracked `.db` files exist;
`.coverage` is tracked and belongs to WT11. Shared review inputs are deliberately
serialized: `tests/test_jobs.py` is WT01 then WT02, and WT08 follows WT01 for any
store interaction. After WT01 and WT02 integrate, exclusive `store.py` and
`tests/test_store.py` ownership transfers to WT08. `SKILL.md` transfers from WT06
to WT10 after integration; there must be no concurrent writers.

## Category boundaries

- **A:** persistence, models, locks, monitoring, retry/time utilities, planner,
  morning/poll jobs and common fixtures; `tests/test_poll_bridge.py` reads C behavior.
- **B:** calendar and location resolution plus its calendar fixture and known venues.
- **C:** routing, MTA/realtime/GTFS/weather, packaged transit data, build script and
  transit fixtures/helpers/docs.
- **D:** CLI/config/format/notification, OpenClaw integration and all skill files.
- **E:** alarm/HA/tomorrow implementation, alarm tests and HA example/docs.
- **F:** platform, Nix, CI, packaging, general examples, repository docs and generated
  coverage artifact.

The exact file-level decisions, including every alarm test and all skill scripts, are
in [source-paths.tsv](source-paths.tsv).