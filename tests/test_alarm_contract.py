"""Tests for alarm_contract.py — v1 schedule wire format + status freshness."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from commutecompass.alarm_contract import (
    DEFAULT_FIRST_LEAD_SECONDS,
    MAX_SNAPSHOT_LENGTH,
    PUBLISH_PAYLOAD_KEYS,
    ScheduleError,
    ScheduleSnapshot,
    build_publish_payload,
    canonical_snapshot,
    compare_schedules,
    decide_commit,
    format_utc,
    new_schedule,
    parse_schedule,
    parse_utc,
    serialize_schedule,
    status_freshness_budget,
    status_observation_fresh,
    validate_publish_payload,
)
from commutecompass.timeutil import NYC_TZ

ID = "6b6e9c724ace4c81a350ef144a7ef39b"
DEADLINE = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
WAKE = DEADLINE - timedelta(seconds=DEFAULT_FIRST_LEAD_SECONDS)
ISSUED = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
CANONICAL = (
    '{"v":1,"id":"6b6e9c724ace4c81a350ef144a7ef39b","issued_at":"2026-11-04T01:00:00Z",'
    '"kind":"commute","wake_at":"2026-11-04T12:23:00Z","deadline_at":"2026-11-04T13:00:00Z",'
    '"next_prep_at":"2026-11-04T12:00:00Z"}'
)


def _commute(**kw: object) -> ScheduleSnapshot:
    args: dict[str, object] = {
        "issued_at": ISSUED, "kind": "commute", "wake_at": WAKE, "deadline_at": DEADLINE,
        "next_prep_at": datetime(2026, 11, 4, 12, 0, tzinfo=UTC), "schedule_id": ID,
    }
    args.update(kw)
    return new_schedule(**args)  # type: ignore[arg-type]


# ─────────── timestamps ───────────

def test_format_utc_canonical_and_microseconds() -> None:
    nyc = datetime(2026, 11, 3, 20, 0, tzinfo=NYC_TZ)  # EST
    assert format_utc(nyc) == "2026-11-04T01:00:00Z"
    assert format_utc(nyc.replace(microsecond=5)) == "2026-11-04T01:00:00.000005Z"
    with pytest.raises(ScheduleError):
        format_utc(datetime(2026, 11, 4, 1, 0))


@pytest.mark.parametrize("raw", [
    "2026-11-04T01:00:00.000000Z",  # zero fraction is non-canonical
    "2026-11-04T01:00:00+00:00",
    "2026-11-04T01:00:00",
    "2026-11-04T01:00:00.123Z",
    "2026-02-30T01:00:00Z",
    "2026-11-04 01:00:00Z",
    " 2026-11-04T01:00:00Z",
    None, 0, "",
])
def test_parse_utc_rejects(raw: object) -> None:
    with pytest.raises(ScheduleError):
        parse_utc(raw)


def test_parse_utc_roundtrip() -> None:
    assert parse_utc("2026-11-04T01:00:00.000005Z") == datetime(
        2026, 11, 4, 1, 0, 0, 5, tzinfo=UTC,
    )


# ─────────── serialization ───────────

def test_serialize_exact_canonical_string() -> None:
    assert serialize_schedule(_commute()) == CANONICAL


def test_serialize_converts_offsets_to_utc() -> None:
    s = _commute(
        issued_at=ISSUED.astimezone(NYC_TZ), wake_at=WAKE.astimezone(NYC_TZ),
        deadline_at=DEADLINE.astimezone(timezone(timedelta(hours=5))),
        next_prep_at=datetime(2026, 11, 4, 7, 0, tzinfo=NYC_TZ),
    )
    assert serialize_schedule(s) == CANONICAL


def test_worst_case_length_is_235() -> None:
    us = timedelta(microseconds=123456)
    s = _commute(issued_at=ISSUED + us, wake_at=WAKE + us, deadline_at=DEADLINE + us,
                 next_prep_at=WAKE + us)
    raw = serialize_schedule(s)
    assert len(raw) == 235 <= MAX_SNAPSHOT_LENGTH
    assert parse_schedule(raw) == s


def test_new_schedule_generates_hex_id() -> None:
    a = _commute(schedule_id=None)
    b = _commute(schedule_id=None)
    assert len(a.id) == 32 and a.id != b.id
    int(a.id, 16)


def test_legacy_and_none_kinds_roundtrip() -> None:
    legacy = new_schedule(issued_at=ISSUED, kind="legacy", wake_at=WAKE, deadline_at=None,
                          next_prep_at=None, schedule_id=ID)
    raw = serialize_schedule(legacy)
    assert '"kind":"legacy"' in raw and '"deadline_at":null' in raw
    assert parse_schedule(raw) == legacy
    none = new_schedule(issued_at=ISSUED, kind="none", wake_at=None, deadline_at=None,
                        next_prep_at=None, schedule_id=ID)
    assert parse_schedule(serialize_schedule(none)) == none


@pytest.mark.parametrize("kw", [
    {"kind": "commute", "deadline_at": None},
    {"kind": "commute", "wake_at": None},
    {"kind": "commute", "deadline_at": DEADLINE + timedelta(seconds=1)},
    {"kind": "legacy"},  # deadline must be null
    {"kind": "legacy", "deadline_at": None, "wake_at": None},
    {"kind": "none", "wake_at": None, "deadline_at": None},  # next_prep must be null
    {"kind": "nap"},
    {"schedule_id": "ABCDEF" * 5 + "AB"},
    {"schedule_id": "abc"},
    {"wake_at": datetime(2026, 11, 4, 12, 23)},
])
def test_new_schedule_rejects_invalid_tuples(kw: dict[str, object]) -> None:
    with pytest.raises(ScheduleError):
        _commute(**kw)


def test_commute_lead_is_configurable() -> None:
    s = _commute(wake_at=DEADLINE - timedelta(minutes=30), first_lead_seconds=1800)
    raw = serialize_schedule(s, first_lead_seconds=1800)
    assert parse_schedule(raw, first_lead_seconds=1800) == s
    with pytest.raises(ScheduleError):
        parse_schedule(raw)  # default 2220 lead


# ─────────── parsing ───────────

def test_parse_canonical() -> None:
    s = parse_schedule(CANONICAL)
    assert s == _commute()
    assert s.issued_at.tzinfo is UTC


def _mutate(**changes: object) -> str:
    body = json.loads(CANONICAL)
    body.update(changes)
    return json.dumps(body, separators=(",", ":"))


@pytest.mark.parametrize("raw", [
    None, "", "unknown", "unavailable", "null", "[]", "1",
    CANONICAL.replace(",", ", "),  # whitespace
    CANONICAL + " ",
    json.dumps(json.loads(CANONICAL), separators=(",", ":"), sort_keys=True),  # key order
    CANONICAL[:-1] + ',"extra":1}',
    CANONICAL.replace('"next_prep_at":"2026-11-04T12:00:00Z"', '"next_prep_at":"x"'),
    CANONICAL.replace('{"v":1,', '{"v":1,"v":1,'),  # duplicate key
    _mutate(v=2),
    _mutate(v=True),
    _mutate(v=1.0),
    _mutate(id=ID.upper()),
    _mutate(id=1),
    _mutate(kind="nap"),
    _mutate(kind="none"),
    _mutate(deadline_at="2026-11-04T13:00:01Z"),
    _mutate(issued_at="2026-11-04T01:00:00.000000Z"),
    _mutate(issued_at=None),
    CANONICAL.replace('"v":1', '"v":NaN'),
    CANONICAL.replace('"kind":"commute"', '"kind":"\\u0063ommute"'),  # escaped ascii
    "{" + "x" * 300 + "}",
])
def test_parse_rejects_noncanonical(raw: object) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(raw)


# ─────────── ordering ───────────

def test_compare_same_id_same_body_is_same() -> None:
    assert compare_schedules(_commute(), parse_schedule(CANONICAL)) == "same"


def test_compare_same_id_different_body_conflicts() -> None:
    other = _commute(next_prep_at=None)
    assert compare_schedules(_commute(), other) == "conflict"


def test_compare_equal_issued_different_id_conflicts() -> None:
    other = _commute(schedule_id="f" * 32)
    assert compare_schedules(_commute(), other) == "conflict"
    # Equal instant expressed in a different zone is still equal.
    nyc = ScheduleSnapshot(**{**other.__dict__, "issued_at": ISSUED.astimezone(NYC_TZ)})
    assert compare_schedules(_commute(), nyc) == "conflict"


def test_compare_older_is_stale_newer_accepted() -> None:
    older = _commute(schedule_id="a" * 32, issued_at=ISSUED - timedelta(microseconds=1))
    newer = _commute(schedule_id="b" * 32, issued_at=ISSUED + timedelta(seconds=1))
    assert compare_schedules(_commute(), older) == "stale"
    assert compare_schedules(_commute(), newer) == "newer"


# ─────────── status freshness ───────────

NOW = datetime(2026, 11, 4, 12, 30, tzinfo=UTC)


def test_freshness_budget() -> None:
    assert status_freshness_budget(30) == timedelta(seconds=90)
    assert status_freshness_budget(60) == timedelta(seconds=180)


@pytest.mark.parametrize("observed, fresh", [
    ("2026-11-04T12:30:00Z", True),
    ("2026-11-04T12:28:30Z", True),  # exactly 90s
    ("2026-11-04T12:28:29Z", False),  # stale
    ("2026-11-04T12:30:00.000001Z", False),  # future
    ("2026-11-04T12:30:00+00:00", False),
    ("2026-11-04T12:30:00.000000Z", False),
    ("garbage", False), ("", False), (None, False), (1, False),
])
def test_status_observation_fresh(observed: object, fresh: bool) -> None:
    assert status_observation_fresh(observed, NOW, 30) is fresh


def test_status_fresh_rejects_naive_now() -> None:
    assert not status_observation_fresh("2026-11-04T12:30:00Z", NOW.replace(tzinfo=None), 30)


def test_status_fresh_accepts_aware_nyc_now() -> None:
    assert status_observation_fresh("2026-11-04T12:30:00Z", NOW.astimezone(NYC_TZ), 30)


def test_status_freshness_independent_of_deadline() -> None:
    # A *currently fresh* observation after a long-passed deadline is usable:
    # the helper takes no deadline (a final ring may run arbitrarily long
    # while the daemon keeps refreshing observed_at).
    late = DEADLINE + timedelta(hours=3)
    assert status_observation_fresh(format_utc(late), late, 30)


# ─────────── script payload + commit decision ───────────


def test_canonical_snapshot_alias() -> None:
    assert canonical_snapshot(_commute()) == CANONICAL


def test_publish_payload_roundtrip() -> None:
    data = build_publish_payload(_commute(), "Class")
    assert data == {"snapshot": CANONICAL, "next_event": "Class"}
    assert validate_publish_payload(data) == _commute()
    assert validate_publish_payload({"snapshot": CANONICAL}) == _commute()


@pytest.mark.parametrize("data", [
    None, [], "x",
    {"snapshot": json.loads(CANONICAL)},  # mapping, not string
    {"next_event": "x"},
    {"snapshot": CANONICAL.replace(",", ", ")},
    {"snapshot": None},
])
def test_validate_publish_payload_rejects(data: object) -> None:
    with pytest.raises(ScheduleError):
        validate_publish_payload(data)


@pytest.mark.parametrize("data", [
    {"snapshot": CANONICAL, "next_event": 1},
    {"snapshot": CANONICAL, "wake_at": "x"},
])
def test_validate_publish_payload_only_snapshot_is_strict(data: object) -> None:
    # HA cannot see undeclared service variables; next_event is display-only.
    assert validate_publish_payload(data) == _commute()
    assert set(build_publish_payload(_commute(), "x")) == PUBLISH_PAYLOAD_KEYS


def test_schedule_error_is_value_error() -> None:
    with pytest.raises(ValueError):
        parse_schedule("unavailable")


def _raw(**kw: object) -> str:
    return serialize_schedule(_commute(**kw))


@pytest.mark.parametrize("current", ["", "unknown", "unavailable"])
def test_decide_commit_empty_helper(current: str) -> None:
    assert decide_commit(current, CANONICAL) == "commit"


def test_decide_commit_cases() -> None:
    later = _raw(schedule_id="b" * 32, issued_at=ISSUED + timedelta(microseconds=1))
    earlier = _raw(schedule_id="a" * 32, issued_at=ISSUED - timedelta(seconds=1))
    equal = _raw(schedule_id="c" * 32)
    same_id_diff = _raw(next_prep_at=None, issued_at=ISSUED + timedelta(seconds=5))
    assert decide_commit(CANONICAL, CANONICAL) == "already_committed"
    assert decide_commit(CANONICAL, later) == "commit"
    assert decide_commit(CANONICAL, earlier) == "stale_or_conflict"
    assert decide_commit(CANONICAL, equal) == "stale_or_conflict"
    assert decide_commit(CANONICAL, same_id_diff) == "stale_or_conflict"
    assert decide_commit("garbage", later) == "stale_or_conflict"
    assert decide_commit(CANONICAL, "garbage") == "invalid"
    assert decide_commit("", CANONICAL.replace(",", ", ")) == "invalid"


def test_decide_commit_orders_by_absolute_instant() -> None:
    # 2026-11-01 DST fall-back: 01:30 EDT (05:30Z) precedes 01:10 EST (06:10Z)
    # even though the NYC wall clock reads earlier for the later instant.
    edt = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0)
    est = datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=1)
    assert est.astimezone(UTC) > edt.astimezone(UTC)
    first = _raw(schedule_id="a" * 32, issued_at=edt)
    second = _raw(schedule_id="b" * 32, issued_at=est)
    assert decide_commit(first, second) == "commit"
    assert decide_commit(second, first) == "stale_or_conflict"
    # Microsecond vs whole-second strings: string order differs from instant order.
    whole = _raw(schedule_id="a" * 32, issued_at=ISSUED + timedelta(seconds=1))
    frac = _raw(schedule_id="b" * 32, issued_at=ISSUED + timedelta(microseconds=500000))
    assert decide_commit(frac, whole) == "commit"
    assert decide_commit(whole, frac) == "stale_or_conflict"


# ─────────── HA script ordering equivalence (pure, mocked) ───────────

def _ha_script_ordering(current_raw: str, snapshot: str) -> str:
    """Python transcription of the documented HA publish script's decisions.

    Mirrors docs/alarm-ha-contract.md step by step:

    1. ``valid``: the canonical validity check runs on ``snapshot`` and on a
       nonempty ``current_raw`` (``''``/``unknown``/``unavailable`` skipped).
       Modelled with :func:`parse_schedule`; ``as_datetime`` is read as the
       canonical-only fail-closed parse.  Failure stops "Invalid alarm
       schedule v1": reported as ``invalid`` when the incoming snapshot is
       bad and ``stale_or_conflict`` (a refusal) when the stored helper is.
    2. "Already committed" stop.
    3. "Stale or conflicting" stop.
    4. Otherwise the single ``input_text.set_value`` commit.
    """
    empty = ("", "unknown", "unavailable")
    try:
        parse_schedule(snapshot)
    except ScheduleError:
        return "invalid"
    if current_raw not in empty:
        try:
            parse_schedule(current_raw)
        except ScheduleError:
            return "stale_or_conflict"
    incoming = json.loads(snapshot)
    try:
        old = json.loads(current_raw)
    except ValueError:
        old = None
    if isinstance(old, dict) and old.get("id") == incoming["id"] and current_raw == snapshot:
        return "already_committed"
    if current_raw not in empty:
        try:
            old_issued = parse_utc(old.get("issued_at")) if isinstance(old, dict) else None
        except ScheduleError:
            old_issued = None
        if (
            not isinstance(old, dict) or old.get("v") != 1 or old_issued is None
            or old.get("id") == incoming["id"]
            or parse_utc(incoming["issued_at"]) <= old_issued
        ):
            return "stale_or_conflict"
    return "commit"


_ORDER_CASES = [
    ("", CANONICAL),
    ("unknown", CANONICAL),
    ("unavailable", CANONICAL),
    (CANONICAL, CANONICAL),  # duplicate id, exact raw → success, no write
    (CANONICAL, serialize_schedule(_commute(next_prep_at=None))),  # same id, other raw
    (CANONICAL, serialize_schedule(  # same id, later issue → still conflict
        _commute(issued_at=ISSUED + timedelta(hours=1)))),
    (CANONICAL, serialize_schedule(_commute(schedule_id="c" * 32))),  # equal issued
    (CANONICAL, serialize_schedule(
        _commute(schedule_id="a" * 32, issued_at=ISSUED - timedelta(microseconds=1)))),
    (CANONICAL, serialize_schedule(
        _commute(schedule_id="b" * 32, issued_at=ISSUED + timedelta(microseconds=1)))),
    ("garbage", CANONICAL),  # malformed nonempty old → fail closed
    ("{}", CANONICAL),
    ('{"v":2,"issued_at":"2026-11-04T00:00:00Z"}', CANONICAL),
    ('{"v":1,"issued_at":"not-a-time"}', CANONICAL),
    ('"x"', CANONICAL),
    # Partial old record: v=1 + parseable issued_at but otherwise malformed.
    ('{"v":1,"issued_at":"2026-11-03T00:00:00Z"}', CANONICAL),
    # Valid-looking but non-canonical old (whitespace) → refused.
    (CANONICAL.replace(",", ", "), serialize_schedule(
        _commute(schedule_id="b" * 32, issued_at=ISSUED + timedelta(hours=1)))),
    # Invalid incoming.
    ("", CANONICAL.replace(",", ", ")),
    (CANONICAL, "garbage"),
]


@pytest.mark.parametrize("current, incoming", _ORDER_CASES)
def test_decide_commit_matches_documented_ha_script(current: str, incoming: str) -> None:
    assert decide_commit(current, incoming) == _ha_script_ordering(current, incoming)


# ─────────── exact strings + lengths per kind ───────────

_US = timedelta(microseconds=123456)
_P = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_HEAD = '{"v":1,"id":"6b6e9c724ace4c81a350ef144a7ef39b",'


@pytest.mark.parametrize("kind, prep, micro, length, tail", [
    ("commute", False, False, 189,
     ('"issued_at":"2026-11-04T01:00:00Z","kind":"commute","wake_at":"2026-11-04T12:23:00Z",'
     '"deadline_at":"2026-11-04T13:00:00Z","next_prep_at":null}')),
    ("commute", False, True, 210,
     ('"issued_at":"2026-11-04T01:00:00.123456Z","kind":"commute",'
     '"wake_at":"2026-11-04T12:23:00.123456Z","deadline_at":"2026-11-04T13:00:00.123456Z",'
     '"next_prep_at":null}')),
    ("commute", True, False, 207,
     ('"issued_at":"2026-11-04T01:00:00Z","kind":"commute","wake_at":"2026-11-04T12:23:00Z",'
     '"deadline_at":"2026-11-04T13:00:00Z","next_prep_at":"2026-11-04T12:00:00Z"}')),
    ("commute", True, True, 235,
     ('"issued_at":"2026-11-04T01:00:00.123456Z","kind":"commute",'
     '"wake_at":"2026-11-04T12:23:00.123456Z","deadline_at":"2026-11-04T13:00:00.123456Z",'
     '"next_prep_at":"2026-11-04T12:00:00.123456Z"}')),
    ("legacy", False, False, 170,
     ('"issued_at":"2026-11-04T01:00:00Z","kind":"legacy","wake_at":"2026-11-04T12:23:00Z",'
     '"deadline_at":null,"next_prep_at":null}')),
    ("legacy", False, True, 184,
     ('"issued_at":"2026-11-04T01:00:00.123456Z","kind":"legacy",'
     '"wake_at":"2026-11-04T12:23:00.123456Z","deadline_at":null,"next_prep_at":null}')),
    ("legacy", True, False, 188,
     ('"issued_at":"2026-11-04T01:00:00Z","kind":"legacy","wake_at":"2026-11-04T12:23:00Z",'
     '"deadline_at":null,"next_prep_at":"2026-11-04T12:00:00Z"}')),
    ("legacy", True, True, 209,
     ('"issued_at":"2026-11-04T01:00:00.123456Z","kind":"legacy",'
     '"wake_at":"2026-11-04T12:23:00.123456Z","deadline_at":null,'
     '"next_prep_at":"2026-11-04T12:00:00.123456Z"}')),
    ("none", False, False, 150,
     ('"issued_at":"2026-11-04T01:00:00Z","kind":"none","wake_at":null,"deadline_at":null,'
     '"next_prep_at":null}')),
    ("none", False, True, 157,
     ('"issued_at":"2026-11-04T01:00:00.123456Z","kind":"none","wake_at":null,'
     '"deadline_at":null,"next_prep_at":null}')),
])
def test_exact_snapshot_and_length_per_kind(
    kind: str, prep: bool, micro: bool, length: int, tail: str,
) -> None:
    m = _US if micro else timedelta(0)
    s = new_schedule(
        issued_at=ISSUED + m, kind=kind,  # type: ignore[arg-type]
        wake_at=None if kind == "none" else WAKE + m,
        deadline_at=DEADLINE + m if kind == "commute" else None,
        next_prep_at=_P + m if prep else None, schedule_id=ID,
    )
    raw = serialize_schedule(s)
    assert raw == _HEAD + tail
    assert len(raw) == length <= MAX_SNAPSHOT_LENGTH
    assert parse_schedule(raw) == s
    # Equivalent to the HA script's canonical check: json with insertion order
    # (no sort_keys), separator spaces stripped, ASCII only.
    ha_canonical = (
        json.dumps(json.loads(raw)).replace(", ", ",").replace(": ", ":")
    )
    assert ha_canonical == raw
    assert raw.isascii()


# ─────────── documented HA declarations (docs/alarm-ha-contract.md) ───────────

_DOC = Path(__file__).resolve().parents[1] / "docs" / "alarm-ha-contract.md"


def _doc_blocks(lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", _DOC.read_text(), flags=re.DOTALL)


@pytest.fixture(scope="module")
def ha_package() -> dict[str, Any]:
    if not _DOC.exists():
        pytest.skip("contract doc not present")
    blocks = [yaml.safe_load(b) for b in _doc_blocks("yaml")]
    pkgs = [b for b in blocks if isinstance(b, dict) and "script" in b]
    assert len(pkgs) == 1, "expected exactly one HA package YAML block"
    return pkgs[0]


def _validity_template(pkg: dict[str, Any]) -> str:
    seq = pkg["script"]["commute_publish_alarm_v1"]["sequence"]
    for step in seq:
        if "variables" in step and "valid" in step["variables"]:
            return str(step["variables"]["valid"])
    raise AssertionError("no 'valid' variable in publish script")


def test_doc_example_service_data_is_canonical() -> None:
    examples = [json.loads(b) for b in _doc_blocks("json")]
    data = next(e for e in examples if isinstance(e, dict) and "snapshot" in e)
    assert validate_publish_payload(data) == _commute()
    assert data["snapshot"] == CANONICAL


def test_doc_helper_and_script_declarations(ha_package: dict[str, Any]) -> None:
    from commutecompass.config import AlarmConfig

    cfg = AlarmConfig()
    helper = cfg.schedule_entity.split(".", 1)
    assert ha_package[helper[0]][helper[1]]["max"] == MAX_SNAPSHOT_LENGTH
    assert "initial" not in ha_package[helper[0]][helper[1]]
    sel = cfg.contract_entity.split(".", 1)
    assert ha_package[sel[0]][sel[1]]["options"] == ["legacy", "v1"]
    script = ha_package["script"][cfg.publish_v1_script.split(".", 1)[1]]
    assert set(script["fields"]) == PUBLISH_PAYLOAD_KEYS
    assert script["fields"]["snapshot"]["required"] is True
    commits = [
        s for s in script["sequence"]
        if s.get("action") == "input_text.set_value"
        and s.get("target", {}).get("entity_id") == cfg.schedule_entity
    ]
    assert len(commits) == 1, "exactly one authoritative helper write"
    assert commits[0]["data"]["value"] == "{{ snapshot }}"


def test_doc_script_validation_constants_match(ha_package: dict[str, Any]) -> None:
    tpl = _validity_template(ha_package)
    keys = re.search(r"set keys = \[(.*?)\]", tpl)
    assert keys is not None
    assert [k.strip().strip("'") for k in keys.group(1).split(",")] == [
        "v", "id", "issued_at", "kind", "wake_at", "deadline_at", "next_prep_at",
    ]
    # Canonical rebuild uses the same insertion order (no sort) and strips spaces.
    rebuild = re.search(r"dict\((.*?)\)\s*\|\s*to_json", tpl, flags=re.DOTALL)
    assert rebuild is not None
    order = [part.split("=")[0].strip() for part in rebuild.group(1).split(",")]
    assert order == ["v", "id", "issued_at", "kind", "wake_at", "deadline_at", "next_prep_at"]
    assert "sort_keys" not in tpl
    assert "replace(', ', ',')" in tpl and "replace(': ', ':')" in tpl
    assert "length <= 255" in tpl
    assert f"== {DEFAULT_FIRST_LEAD_SECONDS}" in tpl
    assert "['commute','legacy','none']" in tpl
    assert "'.000000Z' not in raw" in tpl
    # The validity check covers the incoming snapshot and a nonempty helper.
    assert "for raw in [snapshot, current.raw]" in tpl
    captures = ha_package["script"]["commute_publish_alarm_v1"]["sequence"][0]["variables"]
    # Keep the captured wire text nested so HA native-result conversion cannot
    # turn an all-nonnull JSON record into a mapping before validation.
    assert captures["current"] == "{{ {'raw': states('input_text.commute_alarm_schedule_v1')} }}"
    assert "loop.first or raw not in ['', 'unknown', 'unavailable']" in tpl


def test_doc_script_timestamp_and_id_regex_match_python(ha_package: dict[str, Any]) -> None:
    tpl = _validity_template(ha_package)
    ts = re.search(r"set ts = '(.*?)' %}", tpl)
    ident = re.search(r"p\.id is match\('(.*?)'\)", tpl)
    assert ts is not None and ident is not None
    # Jinja string literal: '\\d' → regex \d.
    ts_re = re.compile(ts.group(1).replace("\\\\", "\\"))
    id_re = re.compile(ident.group(1).replace("\\\\", "\\"))
    for good in ("2026-11-04T01:00:00Z", "2026-11-04T01:00:00.123456Z",
                 format_utc(ISSUED + _US)):
        assert ts_re.match(good)
    for bad in ("2026-11-04T01:00:00+00:00", "2026-11-04T01:00:00.123Z",
                "2026-11-04T01:00:00", "x2026-11-04T01:00:00Z",
                "2026-11-04T24:00:00Z", "2026-11-04T01:60:00Z",
                "2026-11-04T01:00:60Z"):
        assert not ts_re.match(bad)
    assert id_re.match(ID) and not id_re.match(ID + "6") and not id_re.match(ID.upper())


def test_format_utc_zero_pads_small_years() -> None:
    early = datetime(99, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_utc(early) == "0099-01-02T03:04:05Z"
    assert format_utc(early.replace(microsecond=7)) == "0099-01-02T03:04:05.000007Z"
    assert parse_utc(format_utc(early)) == early


def test_status_freshness_rechecked_on_every_use() -> None:
    # The same record is fresh now and stale later: callers must re-evaluate
    # at each use, even past the deadline, and a stale terminal never counts.
    observed = format_utc(DEADLINE + timedelta(hours=2))
    t0 = DEADLINE + timedelta(hours=2, seconds=30)
    assert status_observation_fresh(observed, t0, 30)
    assert not status_observation_fresh(observed, t0 + timedelta(seconds=61), 30)
    # Only a refreshed observation restores usability.
    refreshed = format_utc(t0 + timedelta(seconds=60))
    assert status_observation_fresh(refreshed, t0 + timedelta(seconds=61), 30)


def test_fixture_id_is_32_hex() -> None:
    assert len(ID) == 32 and re.fullmatch(r"[0-9a-f]{32}", ID)


def test_worst_case_snapshot_length_is_235() -> None:
    # 7 '{"v":1,' + 40 id field + 17 kind field + 4 timestamp fields:
    # issued_at 42, wake_at 40, deadline_at 44, next_prep_at 45 (incl. '}'),
    # each timestamp 27 chars (YYYY-MM-DDTHH:MM:SS.ffffffZ) + 2 quotes.
    t = datetime(9998, 12, 31, 23, 0, 0, 999999, tzinfo=UTC)
    s = new_schedule(
        issued_at=t, kind="commute", wake_at=t,
        deadline_at=t + timedelta(seconds=DEFAULT_FIRST_LEAD_SECONDS),
        next_prep_at=t, schedule_id="f" * 32,
    )
    raw = serialize_schedule(s)
    assert len(format_utc(t)) == 27
    assert len(raw) == 7 + 40 + 42 + 17 + 40 + 44 + 45 == 235
    assert len(raw) <= MAX_SNAPSHOT_LENGTH
