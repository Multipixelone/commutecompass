"""Production-shaped Directions → morning persistence → NYCT poll refresh."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest
from bridge_helpers import BOARDING, build_index, directions
from google.transit import gtfs_realtime_pb2 as gtfs

from commutecompass.config import (
    CalendarSpec,
    Config,
    MtaConfig,
    OpencodeGoConfig,
    Origin,
    PathsConfig,
    PrepConfig,
    RealtimeConfig,
    SchedulingConfig,
)
from commutecompass.format import format_prep_ping
from commutecompass.gtfs_index import GtfsIndex
from commutecompass.jobs.morning import run as morning_run
from commutecompass.jobs.poll import run as poll_run
from commutecompass.models import Event, ResolvedLocation
from commutecompass.realtime import _feed_cache
from commutecompass.store import Store
from commutecompass.timeutil import NYC_TZ

OPEN_INDEX = GtfsIndex.open
MORNING = BOARDING - timedelta(hours=1)
POLL = BOARDING - timedelta(minutes=24)


def config_for(tmp_path: Path) -> Config:
    return Config(
        origin=Origin(address="home", lat=40.679364, lon=-73.930729),
        calendars=[CalendarSpec(id="cal", name="Work")],
        prep=PrepConfig(prep_minutes=10, safety_buffer_minutes=5),
        scheduling=SchedulingConfig(),
        paths=PathsConfig(venues_file=str(tmp_path / "venues.yaml"),
                          db_path=str(tmp_path / "state.db"),
                          oauth_token_path=str(tmp_path / "token.json")),
        opencode_go=OpencodeGoConfig(endpoint="https://unused.example"),
        mta=MtaConfig(subway_alerts_url="", lirr_alerts_url="", bus_alerts_url=""),
        realtime=RealtimeConfig(enabled=False, subway_tripupdate_urls=["https://rt.example/A"]),
        google_maps_api_key="fixture",
    )


def nyct_feed(*, opposite: bool = False) -> Any:
    """Actual protobuf presence bits: only departure.time, no sequence/delay/direction."""
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs.FeedHeader.FULL_DATASET
    feed.header.timestamp = int(POLL.timestamp())
    entity = feed.entity.add()
    entity.id = "assigned-A"
    tu = entity.trip_update
    # Extension 1001, NyctTripDescriptor.is_assigned (field 2) = true.
    descriptor = gtfs.TripDescriptor(
        trip_id="050800_A..S" if opposite else "050800_A..N",
        route_id="A", start_date="20261030", start_time="08:27:46",
    )
    tu.trip.ParseFromString(descriptor.SerializeToString() + b"\xca\x3e\x02\x10\x01")
    tu.timestamp = int(POLL.timestamp())
    update = tu.stop_time_update.add()
    update.stop_id = "A48S" if opposite else "A48N"
    update.departure.time = int((BOARDING + timedelta(minutes=6)).timestamp())
    assert not update.HasField("stop_sequence")
    assert not update.departure.HasField("delay")
    assert not tu.trip.HasField("direction_id")
    return gtfs.FeedMessage.FromString(feed.SerializeToString())


@pytest.mark.parametrize("case", ["matched", "missing_index", "bridge_exception", "opposite"])
def test_directions_morning_through_nyct_poll(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(tmp_path)
    if case != "missing_index":
        build_index(Path(config.paths.gtfs_db_path), now=MORNING)
    event = Event(
        id="work", calendar_id="cal", calendar_name="Work", title="Shift",
        start=(BOARDING + timedelta(minutes=30)).astimezone(NYC_TZ),
        end=(BOARDING + timedelta(hours=8)).astimezone(NYC_TZ),
        location_raw="Kingston-Throop Avs",
    )
    resolved = ResolvedLocation(kind="address", value="work", source="geocode",
                                lat=40.679921, lon=-73.940858)
    payload = directions()
    # Provider rounded/adjusted boarding must NOT become the delay baseline.
    payload["routes"][0]["legs"][0]["steps"][1]["transit_details"]["departure_time"]["value"] += 30
    notifier = MagicMock()
    notifier.send.return_value = True

    def open_static(path: str) -> GtfsIndex | None:
        if case == "bridge_exception":
            raise RuntimeError("broken index")
        return OPEN_INDEX(path, now=MORNING)

    with (
        patch("commutecompass.jobs.morning.now_nyc", return_value=MORNING),
        patch("commutecompass.planner.now_nyc", return_value=MORNING),
        patch("commutecompass.timeutil.now_nyc", return_value=MORNING),
        patch("commutecompass.jobs.morning.CalendarClient") as calendar,
        patch("commutecompass.jobs.morning.VenueRegistry.load"),
        patch("commutecompass.jobs.morning.build_notifier", return_value=notifier),
        patch("commutecompass.jobs.morning.fetch_alerts", return_value=[]),
        patch("commutecompass.resolver.resolve", return_value=resolved),
        patch("commutecompass.routing.httpx.Client") as client,
        patch.object(GtfsIndex, "open", side_effect=open_static),
    ):
        calendar.return_value.fetch_events.return_value = [event]
        client.return_value.__enter__.return_value.get.return_value.json.return_value = payload
        morning_run(config)
    store = Store(config.paths.db_path)
    original = store.get_plan(event.id)
    assert original is not None and original.error is None and original.route is not None
    assert original.prep_at is not None and original.leave_at is not None
    boarding = original.route.legs[1]
    trusted = case not in ("missing_index", "bridge_exception")
    assert (boarding.gtfs_provenance is not None) == trusted
    if trusted:
        assert boarding.gtfs_start_time is None
        assert boarding.depart_at.timestamp() == BOARDING.timestamp()
    prep = store.get_pending_ping(event.id, "prep")
    leave = store.get_pending_ping(event.id, "leave")
    assert prep is not None and leave is not None
    # A user's snooze/offset survives realtime refresh, without recreating IDs.
    prep.fire_at += timedelta(minutes=1)
    leave.fire_at += timedelta(minutes=2)
    store.schedule_ping(prep)
    store.schedule_ping(leave)
    config.realtime.enabled = True
    notifier.reset_mock()
    _feed_cache.clear()
    feed = nyct_feed(opposite=case == "opposite")
    # No HTTP occurs below, so TLS/proxy setup must not depend on the host or
    # Nix sandbox. An absent CA file guards against accidentally restoring a
    # real client: construction would fail before the mocked feed fetch runs.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    try:
        with (
            patch("commutecompass.realtime.httpx.Client") as realtime_client,
            patch("commutecompass.realtime.fetch_feed_message", return_value=feed) as fetched,
            patch("commutecompass.timeutil.now_nyc", return_value=POLL),
            patch.object(store, "claim_ping_entry", wraps=store.claim_ping_entry) as claim,
        ):
            poll_run(config, store=store, notifier=notifier, now_fn=lambda: POLL,
                     fetch_alerts_fn=lambda **kw: [], select_alerts_fn=lambda *args, **kw: [])
    finally:
        _feed_cache.clear()
    fetched.assert_called_once_with(
        config.realtime.subway_tripupdate_urls[0],
        boarding.system,
        realtime_client.return_value.__enter__.return_value,
    )
    saved = store.get_plan(event.id)
    assert saved is not None and saved.route == original.route
    if case == "matched":
        assert saved.realtime_buffer_minutes == 6
        assert saved.prep_at == original.prep_at - timedelta(minutes=6)
        assert saved.leave_at == original.leave_at - timedelta(minutes=6)
        pending = store.get_pending_ping(event.id, "leave")
        assert pending is not None and pending.id == leave.id
        assert pending.fire_at.timestamp() == leave.fire_at.timestamp() - 360
        # Newly urgent prep is clamped to now, claimed atomically and dispatched
        # using the refreshed payload, not the morning's scheduled text.
        assert store.get_pending_ping(event.id, "prep") is None
        claim.assert_called_once_with(prep.id, POLL, leave_only=False, due_only=True, defer_if=ANY, suppress_obsolete_prep=True)
        assert notifier.send.call_args.args[0] == format_prep_ping(saved)
        assert notifier.send.call_args.args[0] != prep.message
        with store._connect() as conn:
            row = conn.execute("SELECT fired, fire_at FROM pings WHERE id = ?", (prep.id,)).fetchone()
        assert row[0] == 1
        assert row[1] == POLL.isoformat()
    else:
        assert saved == original
        pending = store.get_pending_ping(event.id, "leave")
        assert pending is not None and pending.fire_at == leave.fire_at
        pending_prep = store.get_pending_ping(event.id, "prep")
        assert pending_prep is not None and pending_prep.fire_at == prep.fire_at
        # Poll may consider future rows in its lookahead; atomic due_only
        # refuses them without consuming either alarm.
        assert all(call.kwargs["due_only"] for call in claim.call_args_list)
        notifier.send.assert_not_called()
