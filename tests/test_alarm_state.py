"""Isolated persistence, binding, and NYC wall-clock tests; no live services."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from commutecompass.alarm_state import (
    ManualWake,
    bind_manual,
    clear_manual,
    clear_manual_for_wake,
    load_last_publish,
    load_manual,
    parse_manual_deadline,
    save_last_publish,
    save_manual,
)
from commutecompass.config import Config, PathsConfig
from commutecompass.store import SCHEMA_VERSION, Store
from commutecompass.timeutil import NYC_TZ


@pytest.fixture
def config(tmp_path: Path) -> Config:
    # This lane only consumes paths; no credentials, services, or origin needed.
    return Config.model_construct(paths=PathsConfig(
        db_path=str(tmp_path / "state.db"),
        venues_file=str(tmp_path / "venues.yaml"),
        oauth_token_path=str(tmp_path / "unused.json"),
    ))


def _nyc(day: int = 5, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=NYC_TZ)


def test_manual_persists_and_is_frozen(config: Config) -> None:
    manual = ManualWake(_nyc(), force=True)
    save_manual(config, manual)
    assert load_manual(config, _nyc(hour=8)) == manual
    assert load_manual(config.model_copy(), _nyc(hour=8)) == manual
    with pytest.raises(FrozenInstanceError):
        manual.force = False  # type: ignore[misc]
    assert Store(config.paths.db_path).schema_version() == SCHEMA_VERSION


def test_expiry_boundary_and_dry_run(config: Config) -> None:
    manual = ManualWake(_nyc())
    save_manual(config, manual)
    bind_manual(config, _nyc(hour=9), manual.deadline_at)
    assert load_manual(config, manual.deadline_at, dry_run=True) is None
    # Dry-run did not actually retire anything, including the binding.
    assert load_manual(config, _nyc(hour=8), dry_run=True) == manual
    assert load_manual(config, manual.deadline_at) is None
    assert load_manual(config, _nyc(hour=8)) is None
    assert not clear_manual_for_wake(config, _nyc(hour=9))


def test_reads_on_missing_or_old_db_do_not_write(config: Config) -> None:
    path = Path(config.paths.db_path)
    assert load_manual(config, _nyc(), dry_run=True) is None
    assert load_last_publish(config) is None
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE existing_data (value TEXT)")
    before = path.read_bytes()
    assert load_manual(config, _nyc(), dry_run=True) is None
    assert load_last_publish(config) is None
    assert path.read_bytes() == before


def test_publication_and_manual_are_independent(config: Config) -> None:
    payload: dict[str, object] = {
        "wake_at": _nyc(hour=8).isoformat(),
        "reason": "manual",
        "nested": {"force": True, "optional": None},
    }
    save_last_publish(config, payload)
    save_manual(config, ManualWake(_nyc()))
    bind_manual(config, _nyc(hour=9), _nyc())
    assert clear_manual_for_wake(config, _nyc(hour=9))
    assert load_last_publish(config) == payload
    save_manual(config, ManualWake(_nyc()))
    clear_manual(config)
    assert load_manual(config, _nyc(hour=8)) is None
    assert load_last_publish(config) == payload
    payload["reason"] = "updated"
    save_last_publish(config, payload)
    assert load_last_publish(config) == payload
    with pytest.raises(TypeError):
        save_last_publish(config, {"unserialized": _nyc()})
    assert load_last_publish(config) == payload


def test_additive_schema_preserves_existing_state(config: Config) -> None:
    store = Store(config.paths.db_path)
    store.init_schema()
    with sqlite3.connect(config.paths.db_path) as conn:
        conn.execute("DROP TABLE alarm_state")
        conn.execute(
            "INSERT INTO alerts_seen (alert_id, event_id, seen_at) VALUES ('kept', 'event', 'today')"
        )
    save_manual(config, ManualWake(_nyc()))
    assert store.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(config.paths.db_path) as conn:
        assert conn.execute("SELECT alert_id FROM alerts_seen").fetchone() == ("kept",)
    store.init_schema()
    assert load_manual(config, _nyc(hour=8)) == ManualWake(_nyc())


def test_binding_compare_and_clear(config: Config) -> None:
    deadline, wake = _nyc(), _nyc(hour=8)
    save_manual(config, ManualWake(deadline))
    assert not clear_manual_for_wake(config, wake)  # Not selected/published yet.
    bind_manual(config, wake, deadline)
    assert not clear_manual_for_wake(config, wake + timedelta(minutes=1))
    assert clear_manual_for_wake(config, wake.astimezone(UTC))
    assert not clear_manual_for_wake(config, wake)
    assert load_manual(config, _nyc(hour=7)) is None


@pytest.mark.parametrize("same_deadline", [False, True])
def test_replaced_intent_survives_stale_completion(config: Config, same_deadline: bool) -> None:
    first, wake = _nyc(), _nyc(hour=8)
    save_manual(config, ManualWake(first))
    bind_manual(config, wake, first)
    replacement = ManualWake(first if same_deadline else _nyc(hour=11), force=True)
    save_manual(config, replacement)
    assert not clear_manual_for_wake(config, wake)
    assert load_manual(config, _nyc(hour=7)) == replacement
    bind_manual(config, _nyc(hour=9), replacement.deadline_at)
    assert clear_manual_for_wake(config, _nyc(hour=9))


def test_wake_may_cross_manual_logical_day_boundary(config: Config) -> None:
    # Logical days begin at 02:00: a one-hour lead crosses into the prior day.
    deadline = _nyc(hour=2, minute=30)
    wake = _nyc(hour=1, minute=30)
    save_manual(config, ManualWake(deadline))
    observed = load_manual(config, _nyc(hour=1))
    assert observed is not None
    bind_manual(config, wake, deadline, expected_manual=observed)
    assert clear_manual_for_wake(config, wake)
    assert load_manual(config, _nyc(hour=1)) is None


@pytest.mark.parametrize("replace_before_bind", [False, True])
def test_calendar_deadline_across_0200_consumes_only_observed_manual(
    config: Config, replace_before_bind: bool
) -> None:
    # A Nov 6 02:30 event leaves at 01:45, across the logical boundary from
    # the selected manual 04:00 deadline. Dismiss/skip must consume that intent.
    manual = ManualWake(_nyc(day=6, hour=4))
    calendar_deadline = _nyc(day=6, hour=1, minute=45)
    wake = _nyc(day=6, hour=1)
    now = _nyc(day=6, hour=0)
    save_manual(config, manual)
    observed = load_manual(config, now)
    assert observed is not None
    if replace_before_bind:
        save_manual(config, manual)  # Even identical replacement has new identity.
    bind_manual(config, wake, calendar_deadline, expected_manual=observed)
    assert clear_manual_for_wake(config, wake) is not replace_before_bind
    assert load_manual(config, now) == (manual if replace_before_bind else None)
    if replace_before_bind:
        current = load_manual(config, now)
        assert current is not None
        bind_manual(config, wake, calendar_deadline, expected_manual=current)
        assert clear_manual_for_wake(config, wake)
        assert load_manual(config, now) is None


@pytest.mark.parametrize("same_values", [False, True])
@pytest.mark.parametrize("calendar_earlier", [False, True])
def test_replacement_before_bind_is_not_consumed(
    config: Config, same_values: bool, calendar_earlier: bool
) -> None:
    first = ManualWake(_nyc())
    save_manual(config, first)
    observed = load_manual(config, _nyc(hour=6))
    assert observed is not None
    replacement = first if same_values else ManualWake(_nyc(hour=11), force=True)
    save_manual(config, replacement)  # Concurrent edit while publishing to HA.
    selected_deadline = _nyc(hour=9) if calendar_earlier else first.deadline_at
    bind_manual(config, _nyc(hour=8), selected_deadline, expected_manual=observed)
    assert not clear_manual_for_wake(config, _nyc(hour=8))
    assert load_manual(config, _nyc(hour=6)) == replacement
    current = load_manual(config, _nyc(hour=6))
    assert current is not None
    bind_manual(config, _nyc(hour=8), selected_deadline, expected_manual=current)
    assert clear_manual_for_wake(config, _nyc(hour=8))


def test_unobserved_binding_cannot_bind_unrelated_deadline(config: Config) -> None:
    save_manual(config, ManualWake(_nyc(hour=11)))
    bind_manual(config, _nyc(hour=8), _nyc(hour=10))
    assert not clear_manual_for_wake(config, _nyc(hour=8))
    bind_manual(config, _nyc(hour=8), _nyc(hour=9), expected_manual=ManualWake(_nyc(hour=11)))
    assert not clear_manual_for_wake(config, _nyc(hour=8))
    # Store callers cannot bypass identity checks with just a deadline token.
    Store(config.paths.db_path).bind_alarm_manual(
        _nyc(hour=8), _nyc(hour=9), expected_deadline_at=_nyc(hour=11)
    )
    assert not clear_manual_for_wake(config, _nyc(hour=8))


@pytest.mark.parametrize("expire", [False, True])
def test_retired_intent_cannot_be_bound_again(config: Config, expire: bool) -> None:
    save_manual(config, ManualWake(_nyc()))
    observed = load_manual(config, _nyc(hour=6))
    assert observed is not None
    if expire:
        assert load_manual(config, _nyc(hour=11)) is None
    else:
        clear_manual(config)
    bind_manual(config, _nyc(hour=8), _nyc(), expected_manual=observed)
    assert not clear_manual_for_wake(config, _nyc(hour=8))
    assert load_manual(config, _nyc(hour=6)) is None


def test_concurrent_completion_has_exactly_one_winner(config: Config) -> None:
    save_manual(config, ManualWake(_nyc()))
    wake = _nyc(hour=8)
    bind_manual(config, wake, _nyc())
    # Separate Store instances/connections, like overlapping daemon processes.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: Store(config.paths.db_path).clear_alarm_manual_for_wake(wake),
            range(2),
        ))
    assert sorted(results) == [False, True]


def test_bind_only_same_logical_day(config: Config) -> None:
    deadline = _nyc(hour=10)
    save_manual(config, ManualWake(deadline))
    bind_manual(config, _nyc(day=6, hour=8), _nyc(day=6, hour=10))
    assert not clear_manual_for_wake(config, _nyc(day=6, hour=8))
    # A calendar-driven earlier deadline can consume this day's observed intent.
    observed = load_manual(config, _nyc(hour=6))
    assert observed is not None
    bind_manual(config, _nyc(hour=7), _nyc(hour=9), expected_manual=observed)
    assert clear_manual_for_wake(config, _nyc(hour=7))
    # Matching schedule dates must not bind a different day's manual intent.
    save_manual(config, ManualWake(deadline))
    bind_manual(config, _nyc(day=6, hour=7), _nyc(day=6, hour=9))
    assert not clear_manual_for_wake(config, _nyc(day=6, hour=7))
    # Midnight belongs to the prior logical day, not the current civil day.
    deadline = _nyc(day=6, hour=1)
    save_manual(config, ManualWake(deadline))
    bind_manual(config, _nyc(day=5, hour=23), deadline)
    assert clear_manual_for_wake(config, _nyc(day=5, hour=23))


@pytest.mark.parametrize("value,hour,minute", [
    ("09:00", 9, 0), ("9am", 9, 0), ("9:30am", 9, 30),
    (" 9:30 PM ", 21, 30), ("12am", 0, 0), ("12pm", 12, 0), ("23:59", 23, 59),
])
def test_parse_formats(value: str, hour: int, minute: int) -> None:
    result = parse_manual_deadline(value, date(2026, 11, 6), _nyc())
    assert result == _nyc(day=6, hour=hour, minute=minute)
    assert result.tzinfo is NYC_TZ


@pytest.mark.parametrize("value", ["25:00", "13am", "0am", "9", "9:30", "09:60", "9:7am", "noon"])
def test_parse_rejects_bad_input(value: str) -> None:
    with pytest.raises(ValueError):
        parse_manual_deadline(value, None, _nyc())


def test_default_strict_next_occurrence_and_explicit_past() -> None:
    now = _nyc(hour=9)
    assert parse_manual_deadline("9am", None, now) == _nyc(day=6, hour=9)
    assert parse_manual_deadline("9:30am", None, now) == _nyc(hour=9, minute=30)
    for day in (date(2026, 11, 4), date(2026, 11, 5)):
        with pytest.raises(ValueError, match="future"):
            parse_manual_deadline("9am", day, now)
    # Pick the NYC date even when now was supplied in UTC.
    assert parse_manual_deadline("11pm", None, _nyc(hour=22).astimezone(UTC)) == _nyc(hour=23)


@pytest.mark.parametrize("explicit", [False, True])
def test_dst_gap_is_rejected(explicit: bool) -> None:
    now = datetime(2026, 3, 8, 0, tzinfo=NYC_TZ)
    with pytest.raises(ValueError, match="DST gap"):
        parse_manual_deadline("02:30", now.date() if explicit else None, now)
    # Tomorrow's next occurrence is invalid too: do not roll past a DST gap.
    with pytest.raises(ValueError, match="DST gap"):
        parse_manual_deadline("02:30", None, datetime(2026, 3, 7, 12, tzinfo=NYC_TZ))


@pytest.mark.parametrize("explicit", [False, True])
def test_dst_fold_selects_earliest_future_absolute_occurrence(explicit: bool) -> None:
    day = date(2026, 11, 1)
    before = datetime(2026, 11, 1, 0, tzinfo=NYC_TZ)
    first = datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0)
    second = first.replace(fold=1)
    selected_date = day if explicit else None
    assert parse_manual_deadline("1:30am", selected_date, before).astimezone(UTC) == first.astimezone(UTC)
    for now in (first, first + timedelta(minutes=15), second.replace(minute=15)):
        result = parse_manual_deadline("1:30am", selected_date, now)
        assert result.astimezone(UTC) == second.astimezone(UTC)
        assert result.fold == 1
    if explicit:
        with pytest.raises(ValueError, match="future"):
            parse_manual_deadline("1:30am", day, second)
    else:
        assert parse_manual_deadline("1:30am", None, second).date() == date(2026, 11, 2)


def test_fold_expiry_uses_absolute_instants(config: Config) -> None:
    deadline = datetime(2026, 11, 1, 1, 15, tzinfo=NYC_TZ, fold=1)
    now = datetime(2026, 11, 1, 1, 45, tzinfo=NYC_TZ, fold=0)
    save_manual(config, ManualWake(deadline))
    loaded = load_manual(config, now)
    assert loaded is not None
    assert loaded.deadline_at.astimezone(UTC) == deadline.astimezone(UTC)
    assert load_manual(config, deadline) is None


def test_naive_input_rejected(config: Config) -> None:
    naive = datetime(2026, 11, 5, 10)
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_manual_deadline("9am", None, naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        save_manual(config, ManualWake(naive))
    with pytest.raises(ValueError, match="timezone-aware"):
        load_manual(config, naive, dry_run=True)
