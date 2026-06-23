"""Tests for mta.py — GTFS-RT alert fetcher and matcher."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from typing import Literal, Protocol, cast

from commutecompass.llm import OpencodeGoClient
from commutecompass.models import Alert, Route, TransitLeg
from commutecompass.mta import (
    _fetch_feed,
    _is_location_specific_alert,
    _build_route_context,
    _time_overlaps,
    _systems_lines_overlap,
    NYC_TZ,
    fetch_alerts,
    alerts_affecting_route,
    select_actionable_alerts,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_aware(dt: datetime) -> datetime:
    """Ensure datetime is NYC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NYC_TZ)
    return dt.astimezone(NYC_TZ)


def subway_leg(
    line: str,
    system: str = "MTA Subway",
    depart_at: datetime | None = None,
    arrive_at: datetime | None = None,
) -> TransitLeg:
    """Build a transit leg for testing."""
    now = make_aware(datetime.now(NYC_TZ))
    return TransitLeg(
        mode="TRANSIT",
        system=system,
        line=line,
        headsign=None,
        depart_at=depart_at or now,
        arrive_at=arrive_at or (now + timedelta(minutes=30)),
        duration_seconds=1800,
        summary=f"{line} from A to B",
    )


def sample_route(
    legs: list[TransitLeg],
    depart_at: datetime | None = None,
    arrive_at: datetime | None = None,
) -> Route:
    """Build a route from legs."""
    now = make_aware(datetime.now(NYC_TZ))
    return Route(
        legs=legs,
        depart_at=depart_at or now,
        arrive_at=arrive_at or (now + timedelta(minutes=60)),
        total_duration_seconds=3600,
        transfers=0,
        raw_provider_payload=None,
    )


def alert_with_period(
    alert_id: str,
    affected_routes: set[str],
    affected_systems: set[str],
    period_start: datetime,
    period_end: datetime | None,
    severity: Literal["INFO", "WARNING", "SEVERE"] = "WARNING",
    header: str = "Test alert",
) -> Alert:
    """Build an Alert with a single active period."""
    return Alert(
        id=alert_id,
        header=header,
        description="Test description",
        affected_routes=affected_routes,
        affected_systems=affected_systems,
        active_periods=[(make_aware(period_start), make_aware(period_end) if period_end else None)],
        severity=severity,
    )


# ─── fetch_alerts tests ───────────────────────────────────────────────────────

class TestFetchAlerts:
    """Tests for fetch_alerts (network + parsing)."""

    def test_parses_valid_protobuf_fixture(self) -> None:
        """fetch_alerts parses the gtfs_rt_sample.pb fixture into Alert models."""
        with patch("commutecompass.mta.httpx.Client") as mock_client_cls:
            # Read the real fixture bytes
            with open("tests/fixtures/gtfs_rt_sample.pb", "rb") as f:
                fixture_bytes = f.read()

            # Build a mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = fixture_bytes

            mock_client = MagicMock()
            mock_client.get.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=None)
            mock_client_cls.return_value = mock_client

            # All three feeds return the same fixture bytes (simplified test setup)
            # In reality each feed has its own protobuf content
            alerts = fetch_alerts(
                "https://example.com/subway.pb",
                "https://example.com/lirr.pb",
                "https://example.com/bus.pb",
                client=mock_client,
            )

        # The fixture contains 2 alerts (C line + A line)
        # Each feed produces both alerts, so with 3 feeds we get 6 total
        # Subway feed alerts are first (system = "MTA Subway")
        subway_alerts = [a for a in alerts if a.id.startswith("MTA Subway")]
        assert len(subway_alerts) == 2, f"Expected 2 subway alerts, got {len(subway_alerts)}: {subway_alerts}"
        # The two subway alerts cover the C and A lines (check the structured
        # field, not the id string, which now derives from the feed entity id).
        all_routes = set().union(*(a.affected_routes for a in subway_alerts))
        assert "C" in all_routes
        assert "A" in all_routes

    def test_filters_entities_without_alerts(self) -> None:
        """Feed entities without alert payload are ignored."""
        from google.transit.gtfs_realtime_pb2 import (  # type: ignore[import-untyped]
            FeedMessage,
            FeedEntity,
        )

        # Build a feed with one alert entity and one trip-update entity
        feed = FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = int(time.time())

        entity = FeedEntity()
        entity.id = "trip-update-1"
        # No alert field set — should be skipped
        feed.entity.append(entity)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = feed.SerializeToString()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        mock_client_cls = MagicMock(return_value=mock_client)
        with patch("commutecompass.mta.httpx.Client", mock_client_cls):
            alerts = fetch_alerts(
                "https://example.com/subway.pb",
                "https://example.com/lirr.pb",
                "https://example.com/bus.pb",
                client=mock_client,
            )

        assert alerts == []

    def test_distinct_alerts_same_route_and_time_get_distinct_ids(self) -> None:
        """Two different alerts on the same route/time must not collapse to one id.

        Exercises the derived-id fallback (feed omits entity ids): the alert text
        is hashed into the id so the ledger doesn't suppress the second alert.
        """
        from google.transit.gtfs_realtime_pb2 import FeedEntity, FeedMessage

        start = int(time.time())

        def _make_entity(header: str) -> FeedEntity:
            ent = FeedEntity()
            ent.id = ""  # force the derived-id fallback
            informed = ent.alert.informed_entity.add()
            informed.route_id = "C"
            period = ent.alert.active_period.add()
            period.start = start
            tr = ent.alert.header_text.translation.add()
            tr.text = header
            return ent

        feed = FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = start
        feed.entity.append(_make_entity("Signal problems at Jay St"))
        feed.entity.append(_make_entity("Sick passenger at Hoyt St"))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = feed.SerializeToString()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("commutecompass.mta.httpx.Client", MagicMock(return_value=mock_client)):
            alerts = fetch_alerts("u", "", "", client=mock_client)

        subway = [a for a in alerts if a.id.startswith("MTA Subway")]
        assert len(subway) == 2
        assert subway[0].id != subway[1].id

    def test_url_construction_with_empty_strings(self) -> None:
        """Empty strings fall back to canonical MTA URLs."""
        with patch("commutecompass.mta.httpx.Client") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b""  # empty feed

            mock_client = MagicMock()
            mock_client.get.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=None)
            mock_client_cls.return_value = mock_client

            # Pass empty strings — should fall back to canonical URLs
            fetch_alerts("", "", "", client=mock_client)

            # Verify all three feeds were requested
            calls = mock_client.get.call_args_list
            assert len(calls) == 3

class TestFetchFeedDiagnostics:
    """Unit tests for _fetch_feed payload diagnostics."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        return mock_client

    def test_rejects_empty_body(self, mock_client: MagicMock) -> None:
        """Empty response body raises ValueError with diagnostics."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_client.get.return_value = mock_response

        with pytest.raises(ValueError, match="Empty response body") as exc_info:
            _fetch_feed("https://example.com/subway.pb", "MTA Subway", mock_client)
        assert "status=200" in str(exc_info.value)
        assert "application/octet-stream" in str(exc_info.value)

    def test_rejects_xml_payload(self, mock_client: MagicMock) -> None:
        """XML/HTML payload raises ValueError with diagnostics."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'<?xml version="1.0"?><Error>Not found</Error>'
        mock_response.headers = {"content-type": "text/xml"}
        mock_client.get.return_value = mock_response

        with pytest.raises(ValueError, match="XML/HTML") as exc_info:
            _fetch_feed("https://example.com/subway.pb", "MTA Subway", mock_client)
        assert "text/xml" in str(exc_info.value)

    def test_rejects_json_payload(self, mock_client: MagicMock) -> None:
        """JSON payload raises ValueError with diagnostics."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"error": "bad request"}'
        mock_response.headers = {"content-type": "application/json"}
        mock_client.get.return_value = mock_response

        with pytest.raises(ValueError, match="JSON") as exc_info:
            _fetch_feed("https://example.com/subway.pb", "MTA Subway", mock_client)
        assert "application/json" in str(exc_info.value)

    def test_rejects_error_xml_lowercase(self, mock_client: MagicMock) -> None:
        """XML payload starting with <error> (lowercase) is rejected."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.content = b"<error>Internal error</error>"
        mock_response.headers = {"content-type": "text/html"}
        mock_client.get.return_value = mock_response

        with pytest.raises(ValueError, match="XML/HTML"):
            _fetch_feed("https://example.com/subway.pb", "MTA Subway", mock_client)


# ─── alerts_affecting_route tests ────────────────────────────────────────────

class TestAlertsAffectingRoute:
    """Tests for alerts_affecting_route — time-window overlap logic."""

    def test_no_alerts_returns_empty(self) -> None:
        """Zero alerts → zero matches."""
        route = sample_route([subway_leg("C")])
        now = make_aware(datetime.now(NYC_TZ))
        result = alerts_affecting_route([], route, now)
        assert result == []

    def test_no_route_legs_returns_empty(self) -> None:
        """Route with no legs → no matches."""
        now = make_aware(datetime.now(NYC_TZ))
        route = Route(legs=[], depart_at=now, arrive_at=now, total_duration_seconds=0)
        alert = alert_with_period(
            "x", {"C"}, {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
        )
        result = alerts_affecting_route([alert], route, now)
        assert result == []

    def test_line_match_without_time_overlap_returns_empty(self) -> None:
        """Alert that covers the C line but time window doesn't overlap route."""
        now = make_aware(datetime.now(NYC_TZ))

        # Route departs in 2 hours
        route = sample_route([
            subway_leg(
                "C",
                depart_at=now + timedelta(hours=2),
                arrive_at=now + timedelta(hours=2, minutes=45),
            )
        ])

        # Alert is only active RIGHT NOW (started 1h ago, ends 30min from now)
        # Route departs in 2h — no overlap
        alert = alert_with_period(
            "c-line-now",
            {"C"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(minutes=30),
        )

        result = alerts_affecting_route([alert], route, now)
        assert result == []

    def test_time_overlap_but_wrong_line_returns_empty(self) -> None:
        """Time window overlaps but line doesn't match — no match."""
        now = make_aware(datetime.now(NYC_TZ))

        # Route uses C line
        route = sample_route([
            subway_leg(
                "C",
                depart_at=now + timedelta(hours=1),
                arrive_at=now + timedelta(hours=1, minutes=45),
            )
        ])

        # Alert is active and covers the A line (not C)
        alert = alert_with_period(
            "a-line-active",
            {"A"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=3),
        )

        result = alerts_affecting_route([alert], route, now)
        assert result == []

    def test_c_line_matched_during_active_period(self) -> None:
        """C line alert active during route time → matched."""
        now = make_aware(datetime.now(NYC_TZ))

        # Route with C leg departs in 30 minutes (overlaps with alert active now)
        route = sample_route([
            subway_leg(
                "C",
                depart_at=now + timedelta(minutes=30),
                arrive_at=now + timedelta(hours=1),
            )
        ])

        # Alert active from 1h ago to 1h from now — covers the route departure
        alert = alert_with_period(
            "c-line-active",
            {"C"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
            header="C line delayed",
        )

        result = alerts_affecting_route([alert], route, now)
        assert len(result) == 1
        assert result[0].id == "c-line-active"
        assert "C" in result[0].header

    def test_multi_leg_route_c_match(self) -> None:
        """Route with multiple legs — C line match is found."""
        now = make_aware(datetime.now(NYC_TZ))

        route = sample_route([
            subway_leg("A", depart_at=now, arrive_at=now + timedelta(minutes=20)),
            subway_leg("C", depart_at=now + timedelta(minutes=20), arrive_at=now + timedelta(minutes=50)),
        ])

        alert = alert_with_period(
            "c-in-multi-leg",
            {"C"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=2),
        )

        result = alerts_affecting_route([alert], route, now)
        assert len(result) == 1

    def test_open_ended_active_period(self) -> None:
        """Alert with no end time (open-ended) overlaps route at check time."""
        now = make_aware(datetime.now(NYC_TZ))

        route = sample_route([
            subway_leg(
                "C",
                depart_at=now + timedelta(minutes=15),
                arrive_at=now + timedelta(minutes=45),
            )
        ])

        # Active period started 2h ago, no end — still active now
        alert = Alert(
            id="open-ended",
            header="C line issue",
            description="Ongoing",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[(make_aware(datetime.now(NYC_TZ) - timedelta(hours=2)), None)],
            severity="WARNING",
        )

        result = alerts_affecting_route([alert], route, now)
        assert len(result) == 1

    def test_multiple_alerts_c_and_a_line(self) -> None:
        """Multiple alerts — C and A line both match."""
        now = make_aware(datetime.now(NYC_TZ))

        route = sample_route([
            subway_leg("C", depart_at=now + timedelta(minutes=10), arrive_at=now + timedelta(minutes=40)),
            subway_leg("A", depart_at=now + timedelta(minutes=40), arrive_at=now + timedelta(hours=1, minutes=10)),
        ])

        c_alert = alert_with_period(
            "c-alert",
            {"C"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
        )
        a_alert = alert_with_period(
            "a-alert",
            {"A"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
        )
        other_alert = alert_with_period(
            "b-alert",
            {"B"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
        )

        result = alerts_affecting_route([c_alert, a_alert, other_alert], route, now)
        assert len(result) == 2
        ids = {a.id for a in result}
        assert "c-alert" in ids
        assert "a-alert" in ids

    def test_walking_leg_ignored(self) -> None:
        """Walking legs do not match transit alerts."""
        now = make_aware(datetime.now(NYC_TZ))

        route = Route(
            legs=[
                TransitLeg(
                    mode="WALKING",
                    system=None,
                    line=None,
                    depart_at=now,
                    arrive_at=now + timedelta(minutes=10),
                    duration_seconds=600,
                    summary="Walk to station",
                ),
                subway_leg("C", depart_at=now + timedelta(minutes=10), arrive_at=now + timedelta(minutes=40)),
            ],
            depart_at=now,
            arrive_at=now + timedelta(minutes=40),
            total_duration_seconds=2400,
        )

        # Only the C line leg matches
        c_alert = alert_with_period(
            "c-alert",
            {"C"},
            {"MTA Subway"},
            datetime.now(NYC_TZ) - timedelta(hours=1),
            datetime.now(NYC_TZ) + timedelta(hours=1),
        )

        result = alerts_affecting_route([c_alert], route, now)
        assert len(result) == 1

    def test_system_wide_alert_matches_all_routes_in_system(self) -> None:
        """Alert with no specific route_id (empty) affects all routes in system."""
        now = make_aware(datetime.now(NYC_TZ))

        route = sample_route([
            subway_leg("C", depart_at=now + timedelta(minutes=10), arrive_at=now + timedelta(minutes=40)),
        ])

        # Alert affects entire MTA Subway system (no specific routes)
        alert = Alert(
            id="system-wide-subway",
            header="Subway system issue",
            description="All lines affected",
            affected_routes=set(),
            affected_systems={"MTA Subway"},
            active_periods=[(
                make_aware(datetime.now(NYC_TZ) - timedelta(hours=1)),
                make_aware(datetime.now(NYC_TZ) + timedelta(hours=1)),
            )],
            severity="WARNING",
        )

        result = alerts_affecting_route([alert], route, now)
        assert len(result) == 1


# ─── Time-overlap unit tests ──────────────────────────────────────────────────

class TestTimeOverlap:
    """Unit tests for _time_overlaps helper."""

    def test_exact_overlap(self) -> None:
        """Periods with same start/end overlap."""
        now = make_aware(datetime.now(NYC_TZ))
        assert _time_overlaps(
            Alert(
                id="x", header="", description="",
                affected_routes=set(), affected_systems=set(),
                active_periods=[(now, now + timedelta(hours=1))],
            ),
            Route(
                legs=[subway_leg("C", depart_at=now, arrive_at=now + timedelta(hours=1))],
                depart_at=now, arrive_at=now + timedelta(hours=1),
                total_duration_seconds=3600,
            ),
            now,
        )

    def test_partial_overlap_at_start(self) -> None:
        """Leg starts during alert period → overlap."""
        now = make_aware(datetime.now(NYC_TZ))
        alert_period_start = now - timedelta(minutes=30)
        alert_period_end = now + timedelta(minutes=30)
        leg_start = now - timedelta(minutes=10)  # leg started 10 min ago

        alert = Alert(
            id="x", header="", description="",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[(alert_period_start, alert_period_end)],
        )
        leg = subway_leg("C", depart_at=leg_start, arrive_at=leg_start + timedelta(minutes=30))
        route = sample_route([leg])

        assert _time_overlaps(alert, route, now)

    def test_leg_starts_after_alert_ends_no_overlap(self) -> None:
        """Leg starts after alert ends → no overlap."""
        now = make_aware(datetime.now(NYC_TZ))
        alert_period_end = now - timedelta(minutes=10)
        leg_start = now + timedelta(minutes=30)  # starts 30 min from now

        alert = Alert(
            id="x", header="", description="",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[(now - timedelta(hours=2), alert_period_end)],
        )
        leg = subway_leg("C", depart_at=leg_start, arrive_at=leg_start + timedelta(minutes=30))
        route = sample_route([leg])

        result = _time_overlaps(alert, route, now)
        assert result is False


# ─── System/line overlap tests ─────────────────────────────────────────────────

class TestSystemsLinesOverlap:
    """Unit tests for _systems_lines_overlap."""

    def test_exact_line_match(self) -> None:
        """Leg line matches alert affected route."""
        now = make_aware(datetime.now(NYC_TZ))
        alert = alert_with_period(
            "x", {"C"}, {"MTA Subway"},
            now - timedelta(hours=1), now + timedelta(hours=1),
        )
        leg = subway_leg("C")
        route = sample_route([leg])
        assert _systems_lines_overlap(alert, route) is True

    def test_no_line_match(self) -> None:
        """Leg line does not match alert route."""
        now = make_aware(datetime.now(NYC_TZ))
        alert = alert_with_period(
            "x", {"A"}, {"MTA Subway"},
            now - timedelta(hours=1), now + timedelta(hours=1),
        )
        leg = subway_leg("C")
        route = sample_route([leg])
        assert _systems_lines_overlap(alert, route) is False

    def test_line_substring_match(self) -> None:
        """Alert route is a prefix of leg line."""
        now = make_aware(datetime.now(NYC_TZ))
        alert = alert_with_period(
            "x", {"ABC"}, {"MTA Subway"},  # alert for "ABC" but route has "C"
            now - timedelta(hours=1), now + timedelta(hours=1),
        )
        leg = subway_leg("C")
        route = sample_route([leg])
        # "C" does not contain "ABC" and "ABC" does not contain "C" — no match
        assert _systems_lines_overlap(alert, route) is False

    def test_wildcard_affected_routes(self) -> None:
        """Wildcard in affected_routes matches any line in system."""
        now = make_aware(datetime.now(NYC_TZ))
        alert = Alert(
            id="wildcard", header="", description="",
            affected_routes={"*"},
            affected_systems={"MTA Subway"},
            active_periods=[(now - timedelta(hours=1), now + timedelta(hours=1))],
        )
        leg = subway_leg("C")
        route = sample_route([leg])
        # System match + wildcard route → True
        assert _systems_lines_overlap(alert, route) is True

    def test_lirr_route_matched(self) -> None:
        """LIRR route matches alert for that route."""
        now = make_aware(datetime.now(NYC_TZ))
        alert = alert_with_period(
            "lirr-alert", {"Atlantic Branch"}, {"LIRR"},
            now - timedelta(hours=1), now + timedelta(hours=1),
            header="LIRR alert",
        )
        leg = subway_leg("Atlantic Branch", system="LIRR")
        route = sample_route([leg])
        assert _systems_lines_overlap(alert, route) is True


class _StubLLM:
    def __init__(self, decision: bool | None) -> None:
        self.decision = decision
        self.calls = 0

    def classify_alert_relevance(self, alert: Alert, route: Route, *, at_time: datetime) -> bool | None:
        self.calls += 1
        return self.decision


class _LLMClientProto(Protocol):
    """Minimal protocol for the llm parameter accepted by select_actionable_alerts."""

    calls: int

    def classify_alert_relevance(self, alert: Alert, route: Route, *, at_time: datetime) -> bool | None: ...


class TestSelectActionableAlerts:
    def test_filters_non_commute_advisory(self) -> None:
        now = make_aware(datetime.now(NYC_TZ))
        route = sample_route([
            subway_leg("C", depart_at=now + timedelta(minutes=10), arrive_at=now + timedelta(minutes=40)),
        ])

        elevator_alert = alert_with_period(
            "elevator-1",
            {"C"},
            {"MTA Subway"},
            now - timedelta(hours=1),
            now + timedelta(hours=1),
            header="Elevator unavailable at 50 St station",
        )

        result = select_actionable_alerts([elevator_alert], route, at_time=now)
        assert result == []

    def test_keeps_disruption_alert_without_llm(self) -> None:
        now = make_aware(datetime.now(NYC_TZ))
        route = sample_route([
            subway_leg("C", depart_at=now + timedelta(minutes=20), arrive_at=now + timedelta(minutes=50)),
        ])
        disruption = alert_with_period(
            "delay-1",
            {"C"},
            {"MTA Subway"},
            now - timedelta(hours=1),
            now + timedelta(hours=2),
            header="C train delays",
        )

        result = select_actionable_alerts([disruption], route, at_time=now)
        assert [a.id for a in result] == ["delay-1"]

    def test_ambiguous_alert_uses_llm_true(self) -> None:
        now = make_aware(datetime.now(NYC_TZ))
        route = sample_route([
            subway_leg("A", depart_at=now + timedelta(minutes=20), arrive_at=now + timedelta(minutes=50)),
        ])
        ambiguous = alert_with_period(
            "ambig-1",
            {"A"},
            {"MTA Subway"},
            now - timedelta(hours=1),
            now + timedelta(hours=2),
            header="A train advisory",
            severity="INFO",
        )
        llm = _StubLLM(True)

        result = select_actionable_alerts([ambiguous], route, at_time=now, llm=cast(OpencodeGoClient, llm))
        assert [a.id for a in result] == ["ambig-1"]
        assert llm.calls == 1

    def test_ambiguous_alert_uses_llm_false(self) -> None:
        now = make_aware(datetime.now(NYC_TZ))
        route = sample_route([
            subway_leg("A", depart_at=now + timedelta(minutes=20), arrive_at=now + timedelta(minutes=50)),
        ])
        ambiguous = alert_with_period(
            "ambig-2",
            {"A"},
            {"MTA Subway"},
            now - timedelta(hours=1),
            now + timedelta(hours=2),
            header="A train advisory",
            severity="INFO",
        )
        llm = _StubLLM(False)

        result = select_actionable_alerts([ambiguous], route, at_time=now, llm=cast(OpencodeGoClient, llm))
        assert result == []
        assert llm.calls == 1


# ─── Location-specific alert filtering tests ─────────────────────────────────


class TestBuildRouteContext:
    """Tests for _build_route_context."""

    def test_extracts_line_ids_and_stops(self) -> None:
        now = make_aware(datetime.now(NYC_TZ))
        legs = [
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="C",
                headsign="Fulton St", depart_at=now, arrive_at=now + timedelta(minutes=30),
                duration_seconds=1800, summary="C from Jay St-MetroTech to Fulton St",
            ),
        ]
        route = sample_route(legs)
        stop_names, line_ids = _build_route_context(route)
        assert "fulton st" in stop_names
        assert "c" in line_ids

    def test_empty_route(self) -> None:
        route = Route(legs=[], depart_at=make_aware(datetime.now(NYC_TZ)),
                     arrive_at=make_aware(datetime.now(NYC_TZ)), total_duration_seconds=0)
        stop_names, line_ids = _build_route_context(route)
        assert stop_names == set()
        assert line_ids == set()


class TestIsLocationSpecificAlert:
    """Tests for _is_location_specific_alert."""

    def test_alert_without_location_patterns_is_not_filtered(self) -> None:
        """Alert with no station/segment phrasing is kept."""
        alert = Alert(
            id="x", header="C train delays", description="Expect delays",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[], severity="WARNING",
        )
        stop_names = {"fulton st"}
        line_ids = {"c"}
        assert _is_location_specific_alert(alert, stop_names, line_ids) is False

    def test_alert_near_route_stops_is_kept(self) -> None:
        """Location-specific alert that mentions a route stop is kept."""
        alert = Alert(
            id="x",
            header="Delays at Fulton St",
            description="Fulton St station affected",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[], severity="WARNING",
        )
        stop_names = {"fulton st"}
        line_ids = {"c"}
        assert _is_location_specific_alert(alert, stop_names, line_ids) is False

    def test_far_location_specific_alert_is_filtered(self) -> None:
        """Location-specific alert for stops unrelated to route is filtered."""
        alert = Alert(
            id="x",
            header="C train delays at 14 St",
            description="Delays at 14 St station",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[], severity="WARNING",
        )
        # Route only uses "Fulton St" stops; 14 St has no token overlap
        stop_names = {"fulton st"}
        line_ids = {"c"}
        assert _is_location_specific_alert(alert, stop_names, line_ids) is True

    def test_severe_alert_remains_location_specific(self) -> None:
        """SEVERE alerts remain location-specific (but never dropped upstream)."""
        alert = Alert(
            id="x",
            header="C line suspended between Jay St-MetroTech and Euclid",
            description="Suspended service",
            affected_routes={"C"}, affected_systems={"MTA Subway"},
            active_periods=[], severity="SEVERE",
        )
        stop_names = {"fulton st"}
        line_ids = {"c"}
        # True: location-specific (kept because SEVERE checked in heuristic)
        assert _is_location_specific_alert(alert, stop_names, line_ids) is True

    def test_system_wide_alert_remains_location_specific(self) -> None:
        """Wildcard alerts remain location-specific (but never dropped upstream)."""
        alert = Alert(
            id="x",
            header="Delays on C line between 34 St and 59 St",
            description="Service change",
            affected_routes={"*"}, affected_systems={"MTA Subway"},
            active_periods=[], severity="WARNING",
        )
        stop_names = {"fulton st"}
        line_ids = {"c"}
        # True: location-specific (kept because affected_routes={"*"} checked upstream)
        assert _is_location_specific_alert(alert, stop_names, line_ids) is True
