"""Commute wake window: fake-clock state machine and daemon tests (no HA/audio)."""

from __future__ import annotations

import json
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
WAKE = D - timedelta(minutes=50)
DEADLINE = D - timedelta(minutes=13)
W = Settings(window=WindowSettings())
LEGACY = Settings()
REQUIET = RingProfile(0.2, 30.0, 60.0)


def m(minutes: float, base: datetime = D) -> datetime:
    """Absolute minutes relative to ``base`` (default D)."""
    return (base.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(NYC_TZ)


def win(departure: datetime | None = D, wake: datetime | None = WAKE, **kw: Any) -> Inputs:
    return Inputs(ha_ok=True, next_alarm=wake, kind="wake", wake_at=wake, wake_known=True,
                  departure_at=departure, departure_known=True, **kw)


def ring_first(settings: Settings = W) -> DaemonState:
    st, _ = step(DaemonState(), m(-51), win(), settings)
    st, acts = step(st, m(-50), win(), settings)
    assert kinds(acts) == ["start", "state:ringing"]
    assert st.phase == "window" and st.departure == D
    return st


def snooze(st: DaemonState, now: datetime, **kw: Any) -> tuple[DaemonState, list[Any]]:
    return step(st, now, win(snooze_pressed_at=now, **kw), W)


# ─────────── Schedule ────────────────────────────────────────────────────────


def test_first_alarm_rings_at_d_minus_50_with_default_profile() -> None:
    st, _ = step(DaemonState(), m(-51), win(), W)
    assert st.pending == WAKE and st.pending_departure == D
    st, acts = step(st, m(-50), win(), W)
    start = [a for a in acts if isinstance(a, StartRing)]
    assert start and start[0].profile is None  # configured 30% -> 100% / 10s ramp


def test_window_timeline_unlimited_five_minute_snoozes_then_final() -> None:
    st = ring_first()
    now = m(-50)
    rerings = 0
    while True:
        st, acts = snooze(st, now + timedelta(seconds=10))
        assert kinds(acts) == ["stop:snooze", "state:snoozed"]
        assert st.snooze_until is not None
        expected = min(now + timedelta(seconds=10, minutes=5), DEADLINE)
        assert st.snooze_until == expected
        now = st.snooze_until
        st, acts = step(st, now, win(), W)
        assert st.mode == "ringing"
        if now == DEADLINE:
            assert st.phase == "final"
            start = [a for a in acts if isinstance(a, StartRing)]
            assert start[0].profile is None  # full first-alarm ramp
            break
        rerings += 1
        assert st.phase == "window"
        start = [a for a in acts if isinstance(a, StartRing)]
        assert start[0].profile == REQUIET
    assert rerings >= 6  # far beyond the legacy max of 2
    assert st.snoozes == rerings + 1


def test_last_snooze_is_shortened_to_end_at_deadline() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-16))
    assert st.snooze_until == DEADLINE  # not D-11
    st, acts = step(st, m(-13.5), win(), W)
    assert acts == [] and st.mode == "snoozed"
    st, acts = step(st, DEADLINE, win(), W)
    assert kinds(acts) == ["start", "state:ringing"] and st.phase == "final"


def test_rering_uses_quiet_hold_profile_and_snooze_still_works_when_loud() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-49))
    st, acts = step(st, m(-44), win(), W)
    assert [a.profile for a in acts if isinstance(a, StartRing)] == [REQUIET]
    # Two minutes in, the re-ring is at full volume; snooze still honoured.
    st, acts = snooze(st, m(-42))
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.snooze_until == m(-37)


def test_ha_stop_during_window_is_a_snooze_not_a_dismissal() -> None:
    st = ring_first()
    st, acts = step(st, m(-49), win(stop_pressed_at=m(-49)), W)
    assert kinds(acts) == ["stop:snooze", "state:snoozed"]
    assert st.phase == "window" and st.snooze_until == m(-44)
    # Stop while snoozed is ignored, the next ring still comes.
    st, acts = step(st, m(-46), win(stop_pressed_at=m(-46)), W)
    assert acts == [] and st.mode == "snoozed"


def test_window_ring_timeout_goes_quiet_but_final_stays_armed() -> None:
    st = ring_first()
    st, acts = step(st, m(-35), win(), W)  # 15 min unanswered
    assert kinds(acts) == ["stop:timeout", "state:snoozed"]
    assert st.snooze_until == DEADLINE
    st, acts = step(st, DEADLINE, win(), W)
    assert st.phase == "final" and kinds(acts) == ["start", "state:ringing"]


def test_ringing_through_deadline_becomes_final_without_restarting() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-20))
    st, _ = step(st, m(-15), win(), W)
    st, acts = step(st, DEADLINE, win(), W)
    assert acts == [] and st.phase == "final" and st.mode == "ringing"


# ─────────── Final alarm ─────────────────────────────────────────────────────


def _final() -> DaemonState:
    st = ring_first()
    st, _ = step(st, m(-35), win(), W)
    st, _ = step(st, DEADLINE, win(), W)
    assert st.phase == "final"
    return st


def test_final_ignores_snooze_stop_and_never_times_out() -> None:
    st = _final()
    for minutes in (-12, -5, 0, 30, 120):
        now = m(minutes)
        st, acts = step(st, now, win(snooze_pressed_at=now, stop_pressed_at=now), W)
        assert acts == [] and st.mode == "ringing" and st.phase == "final"


def test_final_is_dismissed_only_by_desktop_activity() -> None:
    st = _final()
    st, acts = step(st, m(-10), win(activity_at=m(-10)), W)
    assert kinds(acts) == ["stop:activity", "state:idle"]
    assert any(isinstance(a, PublishWindowDone) for a in acts)
    assert st.mode == "idle" and st.phase == "" and st.departure is None
    st, acts = step(st, m(-9), win(), W)
    assert acts == []  # never re-rings


def test_activity_during_window_dismisses_whole_window() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-49))
    st, acts = step(st, m(-47), win(activity_at=m(-47)), W)
    assert kinds(acts) == ["state:idle"]
    assert any(isinstance(a, PublishWindowDone) for a in acts)
    st, acts = step(st, DEADLINE, win(), W)
    assert acts == [] and st.mode == "idle"


def test_master_switch_and_skip_still_end_final() -> None:
    st = _final()
    off, acts = step(st, m(-5), win(enabled=False), W)
    assert off.mode == "idle" and kinds(acts) == ["stop:disabled", "state:idle"]
    skipped, acts = step(st, m(-5), win(skipped_at=WAKE), W)
    assert skipped.mode == "idle" and "stop:cancelled" in kinds(acts)


def test_replan_never_cancels_window_and_only_pulls_deadline_earlier() -> None:
    st = ring_first()
    st, _ = snooze(st, m(-49))
    # Tomorrow published + later departure today: the window stays put.
    later = win(departure=m(10), wake=m(1440 - 50))
    st, acts = step(st, m(-46), later, W)
    assert st.mode == "snoozed" and st.departure == D and acts == []
    # Earlier departure (realtime delay) pulls the deadline in.
    st, _ = step(st, m(-45), win(departure=m(-5), wake=m(-55)), W)
    assert st.departure == m(-5)
    st, _ = step(st, m(-44), win(departure=m(-5), wake=m(-55)), W)
    st, _ = snooze(st, m(-20), departure=m(-5), wake=m(-55))
    assert st.snooze_until == m(-18)  # new deadline D'-13
    st, _ = step(st, m(-18), win(departure=m(-5), wake=m(-55)), W)
    assert st.phase == "final"


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
    feed, clock = [win()], [m(-51)]
    d = _daemon(tmp_path, feed, clock)
    d.run_once()
    clock[0] = m(-50)
    d.run_once()
    clock[0] = m(-20)
    feed[0] = win(snooze_pressed_at=m(-20))
    d.run_once()
    assert d.state.snooze_until == DEADLINE + timedelta(minutes=-2)
    # Restart while HA is down: cache alone must carry D and the deadline.
    clock[0] = m(-14)
    player = ProfilePlayer()
    d2 = _daemon(tmp_path, [Inputs(ha_ok=False)], clock, player)
    assert d2.state.phase == "window" and d2.state.departure == D
    d2.run_once()
    assert player.profiles == [REQUIET]
    clock[0] = m(-13.9)
    d2.run_once()  # re-ringing at D-14; reaching the deadline needs no snooze
    clock[0] = DEADLINE
    d2.run_once()
    assert str(d2.state.phase) == "final"
    clock[0] = m(-12)
    feed2 = [win(snooze_pressed_at=m(-12), stop_pressed_at=m(-12))]
    d2.reader = lambda: feed2[0]
    d2.run_once()
    assert d2.state.mode == "ringing"


def test_restart_while_final_resumes_full_ramp(tmp_path: Path) -> None:
    feed, clock = [win()], [m(-51)]
    d = _daemon(tmp_path, feed, clock)
    d.run_once()
    clock[0] = m(-50)
    d.run_once()
    clock[0] = m(-35)
    d.run_once()
    clock[0] = DEADLINE
    d.run_once()
    assert d.state.phase == "final"
    player = ProfilePlayer()
    clock[0] = m(-11)
    d2 = _daemon(tmp_path, feed, clock, player)
    d2.run_once()
    assert player.profiles == [None]
    assert d2.state.phase == "final" and d2.state.mode == "ringing"


def test_restart_after_deadline_still_rings_final_before_departure(tmp_path: Path) -> None:
    """Daemon down through the whole window: stale limit must not skip the final."""
    feed, clock = [win()], [m(-60)]
    d = _daemon(tmp_path, feed, clock)
    d.run_once()
    assert d.state.pending == WAKE
    clock[0] = m(-8)  # 42 min late, past deadline, before D
    player = ProfilePlayer()
    d2 = _daemon(tmp_path, [Inputs(ha_ok=False)], clock, player)
    d2.run_once()
    assert d2.state.phase == "final" and d2.state.mode == "ringing"
    assert player.profiles == [None]


def test_restart_mid_window_from_ha_helpers_rings_window(tmp_path: Path) -> None:
    clock = [m(-30)]
    d = _daemon(tmp_path, [win()], clock)
    d.run_once()
    assert d.state.phase == "window" and d.state.mode == "ringing"


def test_after_departure_commute_wake_is_skipped(tmp_path: Path) -> None:
    clock = [m(1)]
    player = FakePlayer()
    d = _daemon(tmp_path, [win()], clock, player)
    d.run_once()
    assert d.state.mode == "idle" and player.events == []


def test_activity_publishes_window_done_and_retries(tmp_path: Path) -> None:
    feed, clock = [win()], [m(-50)]
    calls: list[datetime] = []
    ok = [False]

    def done(at: datetime) -> bool:
        calls.append(at)
        return ok[0]

    d = _daemon(tmp_path, feed, clock)
    d.publish_window_done = done
    d.run_once()
    clock[0] = m(-49)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and calls == [m(-49)]
    ok[0] = True
    clock[0] = m(-48)
    d.run_once()
    assert calls == [m(-49), m(-49)]
    clock[0] = m(-47)
    d.run_once()
    assert len(calls) == 2


def test_old_cache_without_window_fields_loads_as_legacy(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "mode": "snoozed", "active": WAKE.isoformat(), "active_kind": "wake",
        "snooze_until": m(-40).isoformat(), "ring_started": m(-50).isoformat(), "snoozes": 1,
    }))
    st = AlarmCache(path).load()
    assert st.mode == "snoozed" and st.phase == "" and st.departure is None


# ─────────── Fallback to legacy ──────────────────────────────────────────────


def test_mismatched_departure_falls_back_to_legacy_alarm() -> None:
    st, _ = step(DaemonState(), m(-51), win(departure=m(7)), W)
    st, _ = step(st, m(-50), win(departure=m(7)), W)
    assert st.mode == "ringing" and st.phase == ""
    st, acts = step(st, m(-49), win(departure=m(7), stop_pressed_at=m(-49)), W)
    assert kinds(acts) == ["stop:stop", "state:idle"]


def test_window_disabled_keeps_legacy_semantics() -> None:
    st, _ = step(DaemonState(), m(-51), win(), LEGACY)
    st, _ = step(st, m(-50), win(), LEGACY)
    assert st.phase == ""
    st, acts = step(st, m(-49), win(stop_pressed_at=m(-49)), LEGACY)
    assert kinds(acts) == ["stop:stop", "state:idle"]


def test_naps_keep_nine_minute_two_snooze_fifteen_minute_semantics() -> None:
    nap = m(-50)
    base = Inputs(ha_ok=True, next_alarm=nap, kind="nap", nap_at=nap, nap_known=True,
                  wake_known=True, departure_at=m(0), departure_known=True)
    st, _ = step(DaemonState(), m(-51), base, W)
    st, _ = step(st, nap, base, W)
    assert st.phase == ""
    st, _ = step(st, m(-49), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-49)}), W)
    assert st.snooze_until == m(-40)  # 9 minutes
    st, _ = step(st, m(-40), base, W)
    st, _ = step(st, m(-39), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-39)}), W)
    st, _ = step(st, m(-30), base, W)
    st, acts = step(st, m(-29), Inputs(**{**base.__dict__, "snooze_pressed_at": m(-29)}), W)
    assert st.mode == "ringing" and acts == []  # max 2 snoozes
    st, acts = step(st, m(-15), base, W)
    assert kinds(acts) == ["stop:timeout", "state:idle"]


def test_legacy_activity_still_stops_non_commute_alarm(tmp_path: Path) -> None:
    feed, clock = [win(departure=None)], [m(-50)]
    player = FakePlayer()
    d = _daemon(tmp_path, feed, clock, player)
    d.run_once()
    assert d.state.phase == ""
    clock[0] = m(-49)
    d.activity()
    d.run_once()
    assert d.state.mode == "idle" and player.events[-1] == "stop"


# ─────────── DST ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("departure", [
    # Fall back: 01:30 EDT, the window spans the repeated hour.
    datetime(2026, 11, 1, 1, 30, tzinfo=NYC_TZ, fold=0),
    datetime(2026, 11, 1, 1, 10, tzinfo=NYC_TZ, fold=1),
    # Spring forward: 03:20 EDT, the window straddles the skipped hour.
    datetime(2026, 3, 8, 3, 20, tzinfo=NYC_TZ),
])
def test_dst_window_uses_absolute_minutes(departure: datetime) -> None:
    wake = m(-50, departure)
    deadline = m(-13, departure)
    inputs = win(departure=departure, wake=wake)
    st, _ = step(DaemonState(), m(-51, departure), inputs, W)
    st, _ = step(st, wake, inputs, W)
    assert st.phase == "window"
    st, _ = step(st, m(-20, departure), Inputs(**{**inputs.__dict__,
                                                    "snooze_pressed_at": m(-20, departure)}), W)
    assert st.snooze_until is not None
    assert st.snooze_until.astimezone(UTC) == m(-15, departure).astimezone(UTC)
    st, _ = step(st, m(-15, departure), inputs, W)
    st, _ = step(st, m(-14, departure), Inputs(**{**inputs.__dict__,
                                                    "snooze_pressed_at": m(-14, departure)}), W)
    assert st.snooze_until is not None
    assert st.snooze_until.astimezone(UTC) == deadline.astimezone(UTC)
    st, _ = step(st, deadline, inputs, W)
    assert st.phase == "final"


# ─────────── Wiring ──────────────────────────────────────────────────────────


def test_inputs_read_departure_helper_only_when_window_enabled() -> None:
    states = {"input_text.commute_departure_utc": {"state": D.astimezone(UTC).isoformat()}}
    on = inputs_from_states(states, AlarmConfig())
    assert on.departure_known and on.departure_at == D
    off = inputs_from_states(states, AlarmConfig(window=AlarmWindowConfig(enabled=False)))
    assert not off.departure_known and off.departure_at is None
    cleared = inputs_from_states(
        {"input_text.commute_departure_utc": {"state": "1970-01-01T00:00:00+00:00"}},
        AlarmConfig(),
    )
    assert cleared.departure_known and cleared.departure_at is None


def test_build_daemon_wires_window_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_construct(
        home_assistant=HomeAssistantConfig(enabled=True, base_url="http://fake"),
        home_assistant_token="fake",
        alarm=AlarmConfig(sound=str(tmp_path / "a.opus"), cache_path=str(tmp_path / "c.json"),
                          stop_on_activity=False),
    )
    monkeypatch.setattr("commutecompass.alarm_daemon.shutil.which", lambda _: "/fake")
    d = build_daemon(config)
    assert d.settings.window == WindowSettings()
    assert d.settings.snooze == timedelta(minutes=9) and d.settings.max_snoozes == 2
    config.alarm.window = AlarmWindowConfig(enabled=False)
    assert build_daemon(config).settings.window is None
