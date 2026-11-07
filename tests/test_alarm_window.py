"""Commute wake window: fake-clock state machine and daemon tests (no HA/audio)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_alarm_daemon import FakePlayer, kinds

from commutecompass.alarm_daemon import (
    AlarmCache,
    AlarmDaemon,
    DaemonState,
    Inputs,
    PublishWindowDone,
    RingProfile,
    Settings,
    StartRing,
    WindowSettings,
    build_daemon,
    inputs_from_states,
    step,
)
from commutecompass.config import AlarmConfig, AlarmWindowConfig, Config, HomeAssistantConfig
from commutecompass.timeutil import NYC_TZ

D = datetime(2026, 10, 15, 8, 0, tzinfo=NYC_TZ)
WAKE = D - timedelta(minutes=37)
DEADLINE = D
W = Settings(window=WindowSettings())
LEGACY = Settings()
REQUIET = RingProfile(0.2, 30.0, 60.0)


def m(minutes: float, base: datetime = D) -> datetime:
    """Absolute minutes relative to ``base`` (default D)."""
    return (base.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(NYC_TZ)


def win(deadline: datetime | None = D, wake: datetime | None = WAKE, **kw: Any) -> Inputs:
    return Inputs(ha_ok=True, next_alarm=wake, kind="wake", wake_at=wake, wake_known=True,
                  deadline_at=deadline, deadline_known=True, **kw)


def ring_first(settings: Settings = W) -> DaemonState:
    st, _ = step(DaemonState(), m(-38), win(), settings)
    st, acts = step(st, m(-37), win(), settings)
    assert kinds(acts) == ["start", "state:ringing"]
    assert st.phase == "window" and st.deadline == D
    return st


def snooze(st: DaemonState, now: datetime, **kw: Any) -> tuple[DaemonState, list[Any]]:
    return step(st, now, win(snooze_pressed_at=now, **kw), W)


# ─────────── Schedule ────────────────────────────────────────────────────────


def test_first_alarm_rings_at_d_minus_37_with_default_profile() -> None:
    st, _ = step(DaemonState(), m(-38), win(), W)
    assert st.pending == WAKE and st.pending_deadline == D
    st, acts = step(st, m(-37), win(), W)
    start = [a for a in acts if isinstance(a, StartRing)]
    assert start and start[0].profile is None  # configured 30% -> 100% / 10s ramp


@pytest.mark.parametrize("control", ["snooze_pressed_at", "stop_pressed_at"])
def test_window_timeline_unlimited_six_and_half_minute_snoozes_then_final(control: str) -> None:
    st = ring_first()
    for pressed_minute, rering_minute in [
        (-36, -29.5), (-29, -22.5), (-22, -15.5),
        (-15, -8.5), (-8, -1.5), (-1, 0),
    ]:
        pressed_at = m(pressed_minute)
        st, acts = step(st, pressed_at, win(**{control: pressed_at}), W)
        assert kinds(acts) == ["stop:snooze", "state:snoozed"]
        assert st.snooze_until == m(rering_minute)
        st, acts = step(st, m(rering_minute), win(), W)
        assert st.mode == "ringing"
        start = [a for a in acts if isinstance(a, StartRing)]
        if rering_minute == 0:
            assert st.phase == "final"
            assert start[0].profile is None  # full first-alarm ramp
        else:
            assert st.phase == "window"
            assert start[0].profile == REQUIET
    assert st.snoozes == 6  # window snoozes exceed the normal/nap max of 2


def test_last_snooze_is_shortened_to_end_at_deadline() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-3))
    assert st.snooze_until == DEADLINE  # not D+3m30s
    st, acts = step(st, m(-0.5), win(), W)
    assert acts == [] and st.mode == "snoozed"
    st, acts = step(st, DEADLINE, win(), W)
    assert kinds(acts) == ["start", "state:ringing"] and st.phase == "final"


def test_rering_uses_quiet_hold_profile_and_snooze_still_works_when_loud() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-36))
    st, acts = step(st, m(-29.5), win(), W)
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [REQUIET]
    # Two minutes in, the re-ring is at full volume; snooze still honoured.
    st, acts = snooze(st, m(-27.5))
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.snooze_until == m(-21)


def test_ha_stop_during_window_is_a_snooze_not_a_dismissal() -> None:
    st = ring_first()
    st, acts = step(st, m(-36), win(stop_pressed_at=m(-36)), W)
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.phase == "window" and st.snooze_until == m(-29.5)
    # Stop while snoozed is ignored, the next ring still comes.
    st, acts = step(st, m(-33), win(stop_pressed_at=m(-33)), W)
    assert acts == [] and st.mode == "snoozed"


def test_window_ring_timeout_repeats_six_and_half_minute_snoozes_then_final() -> None:
    st = ring_first()
    st, acts = step(st, m(-22.01), win(), W)
    assert acts == [] and st.mode == "ringing"  # not yet 15 minutes
    st, acts = step(st, m(-22), win(), W)
    assert kinds(acts) == ["stop:timeout", "state:snoozed"]
    assert st.snooze_until == m(-15.5)
    st, acts = step(st, m(-15.51), win(), W)
    assert acts == [] and st.mode == "snoozed"
    st, acts = step(st, m(-15.5), win(), W)
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [REQUIET]
    assert st.ring_started == m(-15.5)
    st, acts = step(st, m(-0.51), win(), W)
    assert acts == [] and st.mode == "ringing"
    st, acts = step(st, m(-0.5), win(), W)  # another full 15-minute ring
    assert kinds(acts) == ["stop:timeout", "state:snoozed"]
    assert st.snooze_until == DEADLINE  # now+6m30s is capped at D
    st, acts = step(st, DEADLINE, win(), W)
    assert st.phase == "final" and kinds(acts) == ["start", "state:ringing"]
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [None]
    st, acts = step(st, m(120), win(), W)
    assert st.mode == "ringing" and st.phase == "final" and acts == []


@pytest.mark.parametrize("control", ["snooze_pressed_at", "stop_pressed_at"])
def test_manual_control_close_to_deadline_caps_snooze(control: str) -> None:
    st = ring_first()
    st, acts = step(st, m(-0.5), win(**{control: m(-0.5)}), W)
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.snooze_until == D
    st, acts = step(st, D, win(), W)
    assert st.phase == "final" and st.mode == "ringing"
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [None]


def test_ringing_through_deadline_becomes_final_without_restarting() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-7))
    st, _ = step(st, m(-0.5), win(), W)
    st, acts = step(st, DEADLINE, win(), W)
    assert acts == [] and st.phase == "final" and st.mode == "ringing"


# ─────────── Final alarm ─────────────────────────────────────────────────────


def _final() -> DaemonState:
    st = ring_first()
    st, _ = step(st, m(-22), win(), W)
    st, _ = step(st, DEADLINE, win(), W)
    assert st.phase == "final"
    return st


def test_final_ignores_snooze_stop_and_never_times_out() -> None:
    st = _final()
    for minutes in (0, 1, 5, 30, 120):
        now = m(minutes)
        st, acts = step(st, now, win(snooze_pressed_at=now, stop_pressed_at=now), W)
        assert acts == [] and st.mode == "ringing" and st.phase == "final"


def test_final_is_dismissed_only_by_desktop_activity() -> None:
    st = _final()
    st, acts = step(st, m(3), win(activity_at=m(3)), W)
    assert kinds(acts) == ["stop:activity", "state:idle"]
    assert any(isinstance(a, PublishWindowDone) for a in acts)
    assert st.mode == "idle" and st.phase == "" and st.deadline is None
    st, acts = step(st, m(4), win(), W)
    assert acts == []  # never re-rings


def test_activity_during_window_dismisses_whole_window() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-36))
    st, acts = step(st, m(-34), win(activity_at=m(-34)), W)
    assert kinds(acts) == ["state:idle"]
    assert any(isinstance(a, PublishWindowDone) for a in acts)
    st, acts = step(st, DEADLINE, win(), W)
    assert acts == [] and st.mode == "idle"


def test_master_switch_and_skip_still_end_final() -> None:
    st = _final()
    off, acts = step(st, m(8), win(enabled=False), W)
    assert off.mode == "idle" and kinds(acts) == ["stop:disabled", "state:idle"]
    skipped, acts = step(st, m(8), win(skipped_at=WAKE), W)
    assert skipped.mode == "idle" and "stop:cancelled" in kinds(acts)
    cleared, acts = step(st, m(8), win(wake=None), W)
    assert cleared.mode == "idle" and "stop:cancelled" in kinds(acts)


def test_replan_never_cancels_window_and_only_pulls_deadline_earlier() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-36))
    # Tomorrow's wake and a later deadline today cannot extend the window.
    later = win(deadline=m(10), wake=m(1440 - 37))
    st, acts = step(st, m(-35), later, W)
    assert st.mode == "snoozed" and st.deadline == D and acts == []
    earlier = m(-33)
    st, _ = step(st, m(-34), win(deadline=earlier, wake=m(-70)), W)
    assert st.deadline == earlier and st.snooze_until == earlier
    st, acts = step(st, earlier, later, W)
    assert st.deadline == earlier and st.phase == "final"
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [None]


def test_replan_on_another_day_does_not_replace_locked_deadline() -> None:
    st = ring_first()
    st, _ = step(st, m(-36), win(deadline=D - timedelta(days=1)), W)
    assert st.deadline == D and st.phase == "window"


# ─────────── Restart / cache ─────────────────────────────────────────────────


def _daemon(tmp_path: Path, feed: list[Inputs], clock: list[datetime],
            player: FakePlayer | None = None) -> AlarmDaemon:
    return AlarmDaemon(reader=lambda: feed[0], player=player or FakePlayer(),
                       cache=AlarmCache(tmp_path / "c.json"), settings=W,
                       clock=lambda: clock[0])


class ProfilePlayer(FakePlayer):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: list[RingProfile | None] = []

    def start(self, profile: RingProfile | None = None) -> None:
        super().start(profile)
        self.profiles.append(profile)


def test_restart_mid_window_snoozed_keeps_deadline_and_final(tmp_path: Path) -> None:
    feed, clock = [win()], [m(-38)]
    d = _daemon(tmp_path, feed, clock)
    d.run_once()
    clock[0] = m(-37)
    d.run_once()
    clock[0] = m(-7)
    feed[0] = win(snooze_pressed_at=m(-7))
    d.run_once()
    assert d.state.snooze_until == m(-0.5)
    # Restart while HA is down: cache alone must carry D and the deadline.
    clock[0] = m(-0.5)
    player = ProfilePlayer()
    d2 = _daemon(tmp_path, [Inputs(ha_ok=False)], clock, player)
    assert d2.state.phase == "window" and d2.state.deadline == D
    d2.run_once()
    assert player.profiles == [REQUIET]
    clock[0] = m(-0.4)
    d2.run_once()  # re-ringing at D-30s; reaching the deadline needs no snooze
    clock[0] = DEADLINE
    d2.run_once()
    assert str(d2.state.phase) == "final"
    clock[0] = m(1)
    feed2 = [win(snooze_pressed_at=m(1), stop_pressed_at=m(1))]
    d2.reader = lambda: feed2[0]
    d2.run_once()
    assert d2.state.mode == "ringing"


def test_restart_while_final_resumes_full_ramp(tmp_path: Path) -> None:
    feed, clock = [win()], [m(-38)]
    d = _daemon(tmp_path, feed, clock)
    d.run_once()
    clock[0] = m(-37)
    d.run_once()
    clock[0] = m(-22)
    d.run_once()
    clock[0] = DEADLINE
    d.run_once()
    assert d.state.phase == "final"
    player = ProfilePlayer()
    clock[0] = m(2)
    d2 = _daemon(tmp_path, feed, clock, player)
    d2.run_once()
    assert player.profiles == [None]
    assert d2.state.phase == "final" and d2.state.mode == "ringing"


def test_restart_mid_window_from_ha_helpers_rings_window(tmp_path: Path) -> None:
    clock = [m(-17)]
    d = _daemon(tmp_path, [win()], clock)
    d.run_once()
    assert d.state.phase == "window" and d.state.mode == "ringing"


@pytest.mark.parametrize("late", [0, 1, 10])
def test_initial_missed_final_recovers_through_inclusive_bound(tmp_path: Path, late: int) -> None:
    clock = [m(late)]
    player = ProfilePlayer()
    d = _daemon(tmp_path, [win()], clock, player)
    d.run_once()
    assert d.state.mode == "ringing" and d.state.phase == "final"
    assert player.profiles == [None] and d.state.deadline == D
    clock[0] = m(120)
    d.run_once()
    assert d.state.mode == "ringing" and player.profiles == [None]


@pytest.mark.parametrize("late", [10 + 1 / 60, 11, 120])
def test_initial_final_past_recovery_bound_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, late: float,
) -> None:
    clock = [m(late)]
    player = FakePlayer()
    d = _daemon(tmp_path, [win()], clock, player)
    with caplog.at_level("WARNING"):
        d.run_once()
    assert d.state.mode == "idle" and player.events == []
    assert d.state.is_done(WAKE)
    assert "recovery expired" in caplog.text
    d.run_once()
    assert player.events == []


def cache_window(tmp_path: Path, mode: str) -> None:
    """Persist realistic pre-deadline states, then simulate an offline restart."""
    cache = AlarmCache(tmp_path / "c.json")
    if mode == "pending":
        st, _ = step(DaemonState(), m(-38), win(), W)
    else:
        st = ring_first()
        if mode == "snoozed":
            st, _ = snooze(st, m(-36))
        elif mode == "final":
            st, _ = step(st, D, win(), W)
    cache.save(st)


@pytest.mark.parametrize("mode", ["pending", "snoozed", "ringing", "final"])
@pytest.mark.parametrize("late", [0, 1, 10])
def test_restart_missed_final_recovers_through_inclusive_bound(
    tmp_path: Path, mode: str, late: int,
) -> None:
    cache_window(tmp_path, mode)
    clock, player = [m(late)], ProfilePlayer()
    d = _daemon(tmp_path, [Inputs(ha_ok=False)], clock, player)
    d.run_once()
    assert d.state.mode == "ringing" and d.state.phase == "final"
    assert d.state.deadline == D and player.profiles == [None]
    clock[0] = m(120)
    d.run_once()
    assert d.state.mode == "ringing" and player.profiles == [None]


@pytest.mark.parametrize("mode", ["pending", "snoozed", "ringing", "final"])
@pytest.mark.parametrize("late", [10 + 1 / 60, 120])
def test_restart_past_recovery_bound_skips_cached_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str, late: float,
) -> None:
    cache_window(tmp_path, mode)
    player = FakePlayer()
    d = _daemon(tmp_path, [Inputs(ha_ok=False)], [m(late)], player)
    with caplog.at_level("WARNING"):
        d.run_once()
    assert d.state.mode == "idle" and "start" not in player.events
    assert d.state.is_done(WAKE)
    assert "recovery expired" in caplog.text
    d.run_once()
    assert "start" not in player.events


@pytest.mark.parametrize("late", [0, 10, 10 + 1 / 60])
def test_restart_pending_final_survives_tomorrow_helper_rollover(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, late: float,
) -> None:
    cache_window(tmp_path, "pending")  # cached at D-38, before the first wake
    tomorrow = win(deadline=m(1440), wake=m(1440 - 37))
    player = ProfilePlayer()
    d = _daemon(tmp_path, [tomorrow], [m(late)], player)
    with caplog.at_level("WARNING"):
        d.run_once()
    assert d.state.is_done(WAKE)
    if late <= 10:
        assert d.state.mode == "ringing" and d.state.phase == "final"
        assert d.state.active == WAKE and d.state.deadline == D
        assert player.profiles == [None]
        assert "recovery expired" not in caplog.text
    else:
        assert d.state.mode == "idle" and "start" not in player.events
        assert "recovery expired" in caplog.text
        d.run_once()
        assert "start" not in player.events


@pytest.mark.parametrize("deadline,deadline_known", [
    (D - timedelta(days=1), False),
    (D - timedelta(days=1), True),
    (m(-36.5), False),
])
def test_restart_cached_ring_ignores_ineligible_deadline_for_profile(
    tmp_path: Path, deadline: datetime, deadline_known: bool,
) -> None:
    cache_window(tmp_path, "ringing")
    inputs = replace(win(deadline=deadline), deadline_known=deadline_known)
    player = ProfilePlayer()
    d = _daemon(tmp_path, [inputs], [m(-36)], player)
    d.run_once()
    assert d.state.mode == "ringing" and d.state.phase == "window"
    assert d.state.deadline == D and d.state.ring_started == WAKE
    assert player.profiles == [None]  # resume original ring, not a quiet re-ring


@pytest.mark.parametrize("mode", ["ringing", "final"])
@pytest.mark.parametrize("late", [0, 10, 10 + 1 / 60])
def test_restart_failed_audio_stays_latched_without_replay(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str, late: float,
) -> None:
    cache_window(tmp_path, mode)
    cache = AlarmCache(tmp_path / "c.json")
    cache.save(replace(cache.load(), audio_failed=True))
    player = FakePlayer()
    clock = [m(late)]
    d = _daemon(tmp_path, [Inputs(ha_ok=False)], clock, player)
    with caplog.at_level("WARNING"):
        d.run_once()
    assert d.state.audio_failed and "start" not in player.events
    assert (d.state.mode == "idle") == (late > 10)
    assert ("recovery expired" in caplog.text) == (late > 10)
    if late <= 10:
        assert d.state.phase == "final" and d.state.mode == "ringing"
        clock[0] = m(120)
        d.run_once()
        assert d.state.mode == "ringing" and d.state.audio_failed
        assert "start" not in player.events


def test_activity_publishes_window_done_and_retries(tmp_path: Path) -> None:
    feed, clock = [win()], [m(-37)]
    calls: list[datetime] = []
    ok = [False]

    def done(at: datetime) -> bool:
        calls.append(at)
        return ok[0]

    d = _daemon(tmp_path, feed, clock)
    d.publish_window_done = done
    d.run_once()
    clock[0] = m(-36)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and calls == [m(-36)]
    ok[0] = True
    clock[0] = m(-35)
    d.run_once()
    assert calls == [m(-36), m(-36)]
    clock[0] = m(-34)
    d.run_once()
    assert len(calls) == 2


def test_window_completion_survives_offline_restart_and_failed_publish(tmp_path: Path) -> None:
    feed, clock = [win()], [WAKE]
    first_player = FakePlayer()
    d = _daemon(tmp_path, feed, clock, first_player)
    calls: list[datetime] = []

    def fail_publish(at: datetime) -> bool:
        calls.append(at)
        return False

    d.publish_window_done = fail_publish
    d.run_once()
    assert (d.state.mode, first_player.events) == ("ringing", ["start"])

    dismissed_at = m(-36)
    clock[0] = dismissed_at
    feed[0] = Inputs(ha_ok=False)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and first_player.events == ["start", "stop"]
    assert calls == []
    assert d.state.window_done_pending == dismissed_at
    assert d.cache.load().window_done_pending == dismissed_at
    raw = json.loads(d.cache.path.read_text())
    assert raw["window_done_pending"] == dismissed_at.isoformat()

    clock[0] = m(-35)
    player = FakePlayer()
    restarted = _daemon(tmp_path, feed, clock, player)
    outcomes = iter([False, True])

    def publish(at: datetime) -> bool:
        calls.append(at)
        return next(outcomes)

    restarted.publish_window_done = publish
    restarted.run_once()  # still offline: retain completion without publishing
    assert calls == [] and player.events == []
    assert restarted.state.window_done_pending == dismissed_at
    assert restarted.cache.load().window_done_pending == dismissed_at

    feed[0] = win()
    clock[0] = m(-34)
    restarted.run_once()  # HA returns but the first completion write fails
    assert calls == [dismissed_at]
    assert restarted.cache.load().window_done_pending == dismissed_at
    clock[0] = m(-33)
    restarted.run_once()
    assert calls == [dismissed_at, dismissed_at]
    assert restarted.state.window_done_pending is None
    assert restarted.cache.load().window_done_pending is None
    assert json.loads(restarted.cache.path.read_text())["window_done_pending"] is None
    assert player.events == []

    clock[0] = m(-32)
    final_player = FakePlayer()
    again = _daemon(tmp_path, feed, clock, final_player)
    again.publish_window_done = lambda at: calls.append(at) or True
    again.run_once()
    assert calls == [dismissed_at, dismissed_at]  # successful completion is durable
    assert again.state.mode == "idle" and final_player.events == []


def test_old_cache_without_window_fields_loads_as_legacy(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "mode": "snoozed", "active": WAKE.isoformat(), "active_kind": "wake",
        "snooze_until": m(-27).isoformat(), "ring_started": m(-37).isoformat(), "snoozes": 1,
    }))
    st = AlarmCache(path).load()
    assert st.mode == "snoozed" and st.phase == "" and st.deadline is None


# ─────────── Fallback to legacy ──────────────────────────────────────────────


def test_mismatched_deadline_falls_back_to_legacy_alarm() -> None:
    st, _ = step(DaemonState(), m(-38), win(deadline=m(7)), W)
    st, _ = step(st, m(-37), win(deadline=m(7)), W)
    assert st.mode == "ringing" and st.phase == ""
    st, acts = step(st, m(-36), win(deadline=m(7), stop_pressed_at=m(-36)), W)
    assert kinds(acts) == ["stop:stop", "state:idle"]


def test_window_disabled_keeps_legacy_semantics() -> None:
    st, _ = step(DaemonState(), m(-38), win(), LEGACY)
    st, _ = step(st, m(-37), win(), LEGACY)
    assert st.phase == ""
    st, acts = step(st, m(-36), win(stop_pressed_at=m(-36)), LEGACY)
    assert kinds(acts) == ["stop:stop", "state:idle"]


def test_naps_use_normal_snooze_override_and_keep_two_snooze_fifteen_minute_limits() -> None:
    settings = replace(W, snooze=timedelta(minutes=9))
    nap = m(-37)
    base = Inputs(ha_ok=True, next_alarm=nap, kind="nap", nap_at=nap, nap_known=True,
                  wake_known=True, deadline_at=m(0), deadline_known=True)
    st, _ = step(DaemonState(), m(-38), base, settings)
    st, _ = step(st, nap, base, settings)
    assert st.phase == ""
    st, _ = step(st, m(-36), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-36)}), settings)
    assert st.snooze_until == m(-27)  # normal override, not the window's 6m30s
    st, _ = step(st, m(-27), base, settings)
    st, _ = step(st, m(-26), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-26)}), settings)
    st, _ = step(st, m(-17), base, settings)
    st, acts = step(st, m(-16), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-16)}), settings)
    assert st.mode == "ringing" and acts == []  # max 2 snoozes
    st, acts = step(st, m(-2), base, settings)
    assert kinds(acts) == ["stop:timeout", "state:idle"]


def test_legacy_activity_still_stops_non_commute_alarm(tmp_path: Path) -> None:
    feed, clock = [win(deadline=None)], [m(-37)]
    player = FakePlayer()
    d = _daemon(tmp_path, feed, clock, player)
    d.run_once()
    assert d.state.phase == ""
    clock[0] = m(-36)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and player.events[-1] == "stop"


# ─────────── DST ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("deadline", [
    # Fall back: both folds, including a window spanning the repeated hour.
    datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0),
    datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=1),
    # Spring forward: 03:20 EDT, the window straddles the skipped hour.
    datetime(2026, 3, 8, 3, 20, tzinfo=NYC_TZ),
])
def test_dst_window_uses_absolute_minutes(deadline: datetime) -> None:
    wake = m(-37, deadline)
    deadline = m(0, deadline)
    inputs = win(deadline=deadline, wake=wake)
    st, _ = step(DaemonState(), m(-38, deadline), inputs, W)
    st, _ = step(st, wake, inputs, W)
    assert st.phase == "window"
    st, _ = step(st, m(-7, deadline), Inputs(**{**inputs.__dict__,
                                                    "snooze_pressed_at": m(-7, deadline)}), W)
    assert st.snooze_until is not None
    assert st.snooze_until.astimezone(UTC) == m(-0.5, deadline).astimezone(UTC)
    st, _ = step(st, m(-0.5, deadline), inputs, W)
    st, _ = step(st, m(-0.25, deadline), Inputs(**{**inputs.__dict__,
                                                    "snooze_pressed_at": m(-0.25, deadline)}), W)
    assert st.snooze_until is not None
    assert st.snooze_until.astimezone(UTC) == deadline.astimezone(UTC)
    st, _ = step(st, deadline, inputs, W)
    assert st.phase == "final"


# ─────────── Wiring ──────────────────────────────────────────────────────────


def test_cache_round_trips_deadline_keys(tmp_path: Path) -> None:
    cache = AlarmCache(tmp_path / "c.json")
    pending, _ = step(DaemonState(), m(-38), win(), W)
    cache.save(pending)
    raw = json.loads(cache.path.read_text())
    assert raw["pending_deadline"] == D.isoformat()
    assert raw["deadline"] is None
    assert cache.load().pending_deadline == D
    cache.save(ring_first())
    raw = json.loads(cache.path.read_text())
    assert raw["deadline"] == D.isoformat()
    assert raw["pending_deadline"] is None  # first ring consumed the pending schedule
    assert cache.load().deadline == D


def test_inputs_read_deadline_helper_only_when_window_enabled() -> None:
    states = {"input_text.commute_alarm_deadline_utc": {"state": D.astimezone(UTC).isoformat()}}
    on = inputs_from_states(states, AlarmConfig())
    assert on.deadline_known and on.deadline_at == D
    off = inputs_from_states(states, AlarmConfig(window=AlarmWindowConfig(enabled=False)))
    assert not off.deadline_known and off.deadline_at is None
    cleared = inputs_from_states(
        {"input_text.commute_alarm_deadline_utc": {"state": "1970-01-01T00:00:00+00:00"}},
        AlarmConfig(),
    )
    assert cleared.deadline_known and cleared.deadline_at is None


@pytest.mark.parametrize("stop_on_activity,hypridle", [(False, True), (True, False), (True, True)])
def test_build_daemon_wires_window_settings_and_activity_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    stop_on_activity: bool, hypridle: bool,
) -> None:
    config = Config.model_construct(
        home_assistant=HomeAssistantConfig(enabled=True, base_url="http://fake"),
        home_assistant_token="fake",
        alarm=AlarmConfig(sound=str(tmp_path / "a.opus"), cache_path=str(tmp_path / "c.json"),
                          stop_on_activity=stop_on_activity),
    )
    monkeypatch.setattr(
        "commutecompass.alarm_daemon.shutil.which",
        lambda tool: None if tool == "hypridle" and not hypridle else "/fake",
    )
    with caplog.at_level("WARNING"):
        d = build_daemon(config)
    assert ("no desktop activity monitor" in caplog.text) == (not stop_on_activity or not hypridle)
    assert d.settings.window == WindowSettings()
    assert d.settings.window.first_lead == timedelta(minutes=37)
    assert d.settings.window.deadline_lead == timedelta(0)
    assert d.settings.stale_limit == timedelta(minutes=10)
    assert config.alarm.window.deadline_utc_entity == "input_text.commute_alarm_deadline_utc"
    assert d.settings.snooze == timedelta(seconds=390) and d.settings.max_snoozes == 2
    caplog.clear()
    config.alarm.window = AlarmWindowConfig(enabled=False)
    assert build_daemon(config).settings.window is None
    assert "no desktop activity monitor" not in caplog.text
