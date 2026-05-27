"""Tests for ``commutecompass.selector.resolve_event_selector``."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from commutecompass.models import Event, Plan
from commutecompass.selector import SelectorError, resolve_event_selector
from commutecompass.store import Store


# Exit codes mirrored from cli.py for assertion clarity.
EXIT_NOT_FOUND = 65
EXIT_UNRESOLVED = 66


def _make_event(event_id: str, *, title: str, start: datetime) -> Event:
    return Event(
        id=event_id,
        calendar_id="cal",
        calendar_name="Cal",
        title=title,
        start=start,
        end=start + timedelta(hours=1),
    )


def _seeded_store(tmp_db_path: Path, plans: list[Plan]) -> Store:
    store = Store(tmp_db_path)
    store.init_schema()
    for p in plans:
        store.upsert_plan(p)
    return store


def _today_anchor() -> datetime:
    """Return a 'now' inside today's NYC logical day, safe from the 2am boundary.

    Test events are placed at small offsets from this anchor, all guaranteed
    to fall inside ``logical_day_bounds_nyc()`` so ``today_plans()`` returns
    them regardless of when the test suite actually runs.
    """
    from commutecompass.timeutil import logical_day_bounds_nyc

    day_start, _day_end = logical_day_bounds_nyc()
    return day_start + timedelta(hours=8)  # ~10am NYC


def test_next_picks_earliest_upcoming(tmp_db_path: Path) -> None:
    """`next` selects the soonest plan whose start is after `now`."""
    now = _today_anchor()
    e_past = _make_event("a1b2c3d4-evt-past", title="Past", start=now - timedelta(hours=1))
    e_soon = _make_event("a1b2c3d4-evt-soon", title="Soon", start=now + timedelta(hours=1))
    e_late = _make_event("a1b2c3d4-evt-late", title="Late", start=now + timedelta(hours=3))
    store = _seeded_store(
        tmp_db_path,
        [Plan(event=e_past), Plan(event=e_soon), Plan(event=e_late)],
    )

    assert resolve_event_selector("next", store, now=now) == "a1b2c3d4-evt-soon"


def test_next_raises_when_nothing_upcoming(tmp_db_path: Path) -> None:
    """All past events → SelectorError(EXIT_NOT_FOUND)."""
    now = _today_anchor()
    e_past = _make_event("past", title="P", start=now - timedelta(hours=1))
    store = _seeded_store(tmp_db_path, [Plan(event=e_past)])

    with pytest.raises(SelectorError) as exc:
        resolve_event_selector("next", store, now=now)
    assert exc.value.exit_code == EXIT_NOT_FOUND


def test_today_n_picks_one_indexed(tmp_db_path: Path) -> None:
    """`today:2` returns the second plan (1-indexed)."""
    now = _today_anchor()
    e1 = _make_event("11111111-aaaa", title="First", start=now + timedelta(hours=1))
    e2 = _make_event("22222222-bbbb", title="Second", start=now + timedelta(hours=2))
    e3 = _make_event("33333333-cccc", title="Third", start=now + timedelta(hours=3))
    store = _seeded_store(tmp_db_path, [Plan(event=e1), Plan(event=e2), Plan(event=e3)])

    assert resolve_event_selector("today:2", store, now=now) == "22222222-bbbb"


def test_today_n_out_of_range(tmp_db_path: Path) -> None:
    """`today:99` with two plans → EXIT_NOT_FOUND."""
    now = _today_anchor()
    e1 = _make_event("11111111-aaaa", title="First", start=now + timedelta(hours=1))
    store = _seeded_store(tmp_db_path, [Plan(event=e1)])

    with pytest.raises(SelectorError) as exc:
        resolve_event_selector("today:99", store, now=now)
    assert exc.value.exit_code == EXIT_NOT_FOUND


def test_hex_prefix_unique_match(tmp_db_path: Path) -> None:
    """An 8+ hex prefix unique to one plan resolves to its event_id."""
    now = _today_anchor()
    e = _make_event("a1b2c3d4ffff", title="X", start=now + timedelta(hours=1))
    store = _seeded_store(tmp_db_path, [Plan(event=e)])

    assert resolve_event_selector("a1b2c3d4", store, now=now) == "a1b2c3d4ffff"


def test_hex_prefix_ambiguous(tmp_db_path: Path) -> None:
    """Two plans starting with the same 8 hex chars → EXIT_UNRESOLVED."""
    now = _today_anchor()
    e1 = _make_event("a1b2c3d4-aaaa", title="A", start=now + timedelta(hours=1))
    e2 = _make_event("a1b2c3d4-bbbb", title="B", start=now + timedelta(hours=2))
    store = _seeded_store(tmp_db_path, [Plan(event=e1), Plan(event=e2)])

    with pytest.raises(SelectorError) as exc:
        resolve_event_selector("a1b2c3d4", store, now=now)
    assert exc.value.exit_code == EXIT_UNRESOLVED


def test_exact_event_id_match(tmp_db_path: Path) -> None:
    """A non-hex literal id is matched exactly."""
    now = _today_anchor()
    e = _make_event("evt-abc", title="X", start=now + timedelta(hours=1))
    store = _seeded_store(tmp_db_path, [Plan(event=e)])

    assert resolve_event_selector("evt-abc", store, now=now) == "evt-abc"


def test_fuzzy_title_confident_match(tmp_db_path: Path) -> None:
    """A clear title fragment resolves via rapidfuzz."""
    now = _today_anchor()
    e1 = _make_event("id-1", title="Daily Standup", start=now + timedelta(hours=1))
    e2 = _make_event("id-2", title="Lunch with Sam", start=now + timedelta(hours=2))
    store = _seeded_store(tmp_db_path, [Plan(event=e1), Plan(event=e2)])

    assert resolve_event_selector("standup", store, now=now) == "id-1"


def test_fuzzy_title_ambiguous(tmp_db_path: Path) -> None:
    """Two near-identical titles → EXIT_UNRESOLVED."""
    now = _today_anchor()
    e1 = _make_event("id-1", title="Standup North", start=now + timedelta(hours=1))
    e2 = _make_event("id-2", title="Standup South", start=now + timedelta(hours=2))
    store = _seeded_store(tmp_db_path, [Plan(event=e1), Plan(event=e2)])

    with pytest.raises(SelectorError) as exc:
        resolve_event_selector("standup", store, now=now)
    assert exc.value.exit_code == EXIT_UNRESOLVED


def test_no_match_returns_input_as_raw_id(tmp_db_path: Path) -> None:
    """Selector that doesn't match anything in today's plans is passed through.

    This is the friendly-fallback path: ``adjust foo --add-prep 10`` should
    surface ``foo``'s "No plan found" message from the downstream command
    rather than an opaque selector error.
    """
    now = _today_anchor()
    store = _seeded_store(tmp_db_path, [])
    # No plans → no fuzzy candidates → input echoes back.
    assert resolve_event_selector("nope", store, now=now) == "nope"


def test_empty_selector_raises(tmp_db_path: Path) -> None:
    """Empty / whitespace selector → EXIT_NOT_FOUND."""
    now = _today_anchor()
    store = _seeded_store(tmp_db_path, [])

    with pytest.raises(SelectorError) as exc:
        resolve_event_selector("   ", store, now=now)
    assert exc.value.exit_code == EXIT_NOT_FOUND
