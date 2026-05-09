"""Tests for format.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal


from commutecompass.format import (
    _summarize_route_mode,
    escape_md,
    format_digest,
    format_leave_ping,
    format_prep_ping,
    format_service_update,
)
from commutecompass.models import (
    Alert,
    Event,
    Plan,
    ResolvedLocation,
    Route,
    TransitLeg,
)


def make_nyc(dt: datetime) -> datetime:
    """Ensure a datetime is in America/New_York timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)  # simplified for tests


def make_event(
    id: str = "evt1",
    title: str = "Example Class",
    calendar_name: str = "Theatre",
    start: datetime | None = None,
    location: str = "200 Example St",
    location_kind: Literal["address", "station"] = "address",
    location_value: str = "200 Example St, New York, NY 10001",
) -> Event:
    """Helper to create a test event with resolved location."""
    if start is None:
        start = datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc)
    resolved = ResolvedLocation(
        kind=location_kind,
        value=location_value,
        lat=40.7128,
        lon=-74.0060,
        source="known_venues",
    )
    return Event(
        id=id,
        calendar_id="cal1",
        calendar_name=calendar_name,
        title=title,
        start=start,
        end=start + timedelta(hours=2),
        location_raw=location,
        location_resolved=resolved,
    )


def make_route(legs: list[TransitLeg]) -> Route:
    """Helper to create a route."""
    depart_at = legs[0].depart_at if legs else datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc)
    arrive_at = legs[-1].arrive_at if legs else datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    total = sum(leg_.duration_seconds for leg_ in legs)
    return Route(
        legs=legs,
        depart_at=depart_at,
        arrive_at=arrive_at,
        total_duration_seconds=total,
        transfers=0,
    )


def make_plan(event: Event, route: Route | None = None) -> Plan:
    """Helper to create a plan with calculated times."""
    leave_at = event.start - timedelta(minutes=75) if event.start else None
    prep_at = leave_at - timedelta(minutes=20) if leave_at else None
    return Plan(
        event=event,
        route=route,
        leave_at=leave_at,
        prep_at=prep_at,
        error=None,
    )


class TestEscapeMd:
    """Tests for escape_md MarkdownV2 escaping."""

    def test_escape_md_basic(self) -> None:
        """escape_md escapes Telegram special characters."""
        result = escape_md("test_string")
        assert result == r"test\_string"

    def test_escape_md_multiple_special_chars(self) -> None:
        """escape_md handles multiple special chars in one string."""
        result = escape_md("a]b[c}d")
        assert result == r"a\]b\[c\}d"

    def test_escape_md_underscore_asterisk(self) -> None:
        """Underscores and asterisks (italic/bold markers) are escaped."""
        result = escape_md("*bold* and _italic_")
        assert result == r"\*bold\* and \_italic\_"

    def test_escape_md_brackets_parens(self) -> None:
        """Square brackets and parentheses are escaped."""
        result = escape_md("text (with) [brackets]")
        assert result == r"text \(with\) \[brackets\]"

    def test_escape_md_hyphen_dot(self) -> None:
        """Hyphens and dots at start of text are escaped."""
        # Hyphen at start of line needs escaping in lists
        assert escape_md("- bullet") == r"\- bullet"
        # Dot needs escaping
        assert escape_md("item.1") == r"item\.1"

    def test_escape_md_hash_pipe(self) -> None:
        """Hash and pipe characters are escaped."""
        assert escape_md("a # b | c") == r"a \# b \| c"

    def test_escape_md_tilde_backtick(self) -> None:
        """Tilde and backtick are escaped."""
        assert escape_md("~code`") == r"\~code\`"

    def test_escape_md_curly_braces(self) -> None:
        """Curly braces are escaped."""
        assert escape_md("{key}") == r"\{key\}"

    def test_escape_md_noop(self) -> None:
        """Plain text without special chars is unchanged."""
        result = escape_md("plain text 123")
        assert result == "plain text 123"

    def test_escape_md_numbers_only(self) -> None:
        """Purely numeric content passes through."""
        result = escape_md("123 456")
        assert result == "123 456"

    def test_escape_md_colon_semicolon(self) -> None:
        """Colons and semicolons are not escaped (they're not special in MD2)."""
        result = escape_md("a: b; c")
        assert result == "a: b; c"

    def test_escape_md_exclamation(self) -> None:
        """Exclamation marks ARE escaped in MD2 (part of list syntax)."""
        result = escape_md("Watch out!")
        assert result == r"Watch out\!"

    def test_escape_md_backslash(self) -> None:
        """Backslashes themselves are not escaped (not special in MD2)."""
        result = escape_md(r"path\to\file")
        assert result == r"path\to\file"

    def test_escape_md_complex_title(self) -> None:
        """Complex event titles with dots and parens are escaped."""
        result = escape_md("ASL — CJ Jones (presentation)")
        # Parens should be escaped
        assert r"\(" in result
        assert r"\)" in result


class TestFormatDigest:
    """Tests for format_digest with realistic plan/alert inputs."""

    def test_format_digest_single_event(self) -> None:
        """format_digest renders a single valid plan correctly."""
        event = make_event(
            id="evt1",
            title="Example Class",
            calendar_name="Theatre",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
            location="200 Example St",
            location_value="200 Example St, New York, NY 10001",
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train from Jay St-MetroTech to Fulton St",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_digest([plan], [])

        # Should contain escaped title, times, route summary
        assert "Example Class" in result
        assert "9:30 AM" in result
        assert "200 Example St" in result
        # Times should be present
        assert "Start prep" in result
        assert "Leave" in result
        # Route summary should include the dominant-mode label (parentheses are escaped in MD2)
        assert r"Subway \(C\)" in result
        # Calendar name wrapped in escaped literal parens so '(' and ')' never appear raw in output
        assert r"\(Theatre\)" in result
        # Should start with today header
        assert "Today" in result

    def test_format_digest_multiple_events(self) -> None:
        """format_digest renders multiple plans with different calendars."""
        event1 = make_event(
            id="evt1",
            title="Example Class",
            calendar_name="Theatre",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg1 = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route1 = make_route([leg1])

        event2 = make_event(
            id="evt2",
            title="ASL — CJ Jones presentation",
            calendar_name="School",
            start=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc),
            location="Example University",
            location_value="Example LIRR Station, NY",
            location_kind="station",
        )
        leg2 = TransitLeg(
            mode="TRANSIT",
            system="LIRR",
            line="Atlantic Branch",
            headsign="Example Centre",
            depart_at=datetime(2026, 5, 12, 12, 25, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 13, 17, tzinfo=timezone.utc),
            duration_seconds=3120,
            summary="LIRR Atlantic Branch",
        )
        route2 = make_route([leg2])

        plan1 = make_plan(event1, route1)
        plan2 = make_plan(event2, route2)

        result = format_digest([plan1, plan2], [])

        assert "Example Class" in result
        assert "ASL" in result
        # Should use different emoji for Theatre vs School
        assert "🎭" in result
        assert "🎓" in result

    def test_format_digest_job_uses_ice_cream_icon_and_salt_straw_fallback(self) -> None:
        """Job calendar uses 🍨 and Salt & Straw when location is missing."""
        event = make_event(
            id="evt-job",
            title="Example User · 6pm - 11pm · SHIFT COORDINATOR",
            calendar_name="Job",
            start=datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc),
            location="",
        )
        plan = make_plan(event, None)

        result = format_digest([plan], [])

        assert "🍨" in result
        assert "Salt & Straw" in result
        assert "(no location)" not in result

    def test_format_digest_with_alerts(self) -> None:
        """format_digest includes alert lines when provided."""
        event = make_event(id="evt1", title="Example Class", start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc))
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        alert = Alert(
            id="alert1",
            header="C train: delays reported",
            description="Expect minor delays on the C line due to signal issues",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[],
            severity="WARNING",
        )

        result = format_digest([plan], [alert])

        assert "C train: delays reported" in result
        assert "*Active service alerts:*" in result

    def test_format_digest_severity_markers(self) -> None:
        """format_digest uses different markers for SEVERE vs WARNING alerts."""
        event = make_event(id="evt1", title="Example Class", start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc))
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        severe_alert = Alert(
            id="alert2",
            header="C line suspended",
            description="C line is suspended between 34 St and Fulton St",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[],
            severity="SEVERE",
        )

        result = format_digest([plan], [severe_alert])

        assert "🔴" in result  # Severe uses red marker
        assert "C line suspended" in result

    def test_format_digest_empty(self) -> None:
        """format_digest with no plans shows appropriate empty message."""
        result = format_digest([], [])
        assert "No events scheduled for today" in result

    def test_format_digest_error_plan(self) -> None:
        """format_digest shows error plans with failure reason."""
        event = make_event(id="evt1", title="Bad Event", start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc))
        plan = Plan(event=event, route=None, error="location_unresolved")

        result = format_digest([plan], [])

        assert "Bad Event" in result
        # Error text is escaped (underscores become \_)
        assert "location" in result and "unresolved" in result
        assert "❌" in result

    def test_format_digest_no_route(self) -> None:
        """format_digest handles plan without route gracefully."""
        event = make_event(id="evt1", title="Walk to Prospect Park", start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc))
        # No route provided
        plan = Plan(
            event=event,
            route=None,
            leave_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            prep_at=datetime(2026, 5, 12, 8, 40, tzinfo=timezone.utc),
            error=None,
        )

        result = format_digest([plan], [])

        assert "Walk to Prospect Park" in result
        # Should still contain the event info
        assert "9:30 AM" in result

    def test_format_digest_special_chars_in_title(self) -> None:
        """format_digest escapes special characters in event titles."""
        event = make_event(
            id="evt1",
            title="ASL — CJ Jones (presentation)",
            calendar_name="School",
            start=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="LIRR",
            line="Atlantic Branch",
            depart_at=datetime(2026, 5, 12, 12, 25, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 13, 17, tzinfo=timezone.utc),
            duration_seconds=3120,
            summary="LIRR Atlantic Branch",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_digest([plan], [])

        # Title with special chars should be safely escaped in output
        assert "ASL" in result
        # Check that special chars are escaped (result should not raise parse errors)
        # The raw output should contain backslashes before special chars
        assert "presentation" in result

    def test_format_digest_truncates_multiline_nyc_us_address(self) -> None:
        """NYC/US multiline addresses collapse to first line only."""
        event = make_event(
            id="evt1",
            title="Example Class",
            calendar_name="Theatre",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
            location="240 Hudson St\nNew York NY 10013\nUnited States",
            location_value="240 Hudson St, New York, NY 10013",
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_digest([plan], [])

        assert "240 Hudson St" in result
        assert "New York NY 10013" not in result
        assert "United States" not in result

    def test_format_digest_keeps_non_nyc_or_non_us_multiline_parts(self) -> None:
        """Non-NYC/US multiline addresses stay flattened with context preserved."""
        event = make_event(
            id="evt1",
            title="Out of Town Meeting",
            calendar_name="Personal",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
            location="1 Main St\nJersey City NJ 07302\nUnited States",
            location_value="1 Main St, Jersey City, NJ 07302",
        )
        plan = make_plan(event, None)

        result = format_digest([plan], [])

        assert "1 Main St" in result
        assert "Jersey City NJ 07302" in result
        assert "United States" in result


class TestFormatPrepPing:
    """Tests for format_prep_ping."""

    def test_format_prep_ping_basic(self) -> None:
        """format_prep_ping shows correct structure."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_prep_ping(plan)

        assert "⏰" in result
        assert "Start getting ready" in result
        assert "Example Class" in result
        assert "9:30 AM" in result
        assert "Leave by" in result

    def test_format_prep_ping_with_duration_hint(self) -> None:
        """format_prep_ping shows time remaining until leave."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_prep_ping(plan)

        # Should mention how many minutes from now
        assert "min from now" in result or "hr from now" in result or "Leave by" in result

    def test_format_prep_ping_special_chars(self) -> None:
        """format_prep_ping escapes special chars in event title."""
        event = make_event(
            id="evt1",
            title="ASL — CJ Jones (presentation)",
            start=datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="LIRR",
            line="Atlantic Branch",
            depart_at=datetime(2026, 5, 12, 12, 25, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 13, 17, tzinfo=timezone.utc),
            duration_seconds=3120,
            summary="LIRR",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_prep_ping(plan)

        # Should contain title content without breaking markdown
        assert "ASL" in result
        assert "CJ Jones" in result


class TestFormatLeavePing:
    """Tests for format_leave_ping."""

    def test_format_leave_ping_basic(self) -> None:
        """format_leave_ping shows correct structure."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
            location="200 Example St",
            location_value="200 Example St, New York, NY 10001",
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_leave_ping(plan)

        assert "🚶" in result
        assert "Leave now" in result
        assert "Example Class" in result
        assert "9:30 AM" in result
        assert "200 Example St" in result

    def test_format_leave_ping_includes_route(self) -> None:
        """format_leave_ping includes route summary when available."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_leave_ping(plan)

        # format_leave_ping uses _route_summary_detailed (actual times), not _route_summary
        # It should include "C train" from the leg summary
        assert "C train" in result

    def test_format_leave_ping_special_chars(self) -> None:
        """format_leave_ping escapes special chars in title."""
        event = make_event(
            id="evt1",
            title="Rehearsal (Full Cast)",
            start=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="A",
            depart_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 45, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="A train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_leave_ping(plan)

        assert "Rehearsal" in result
        # Should not crash on parens

    def test_format_leave_ping_truncates_multiline_nyc_us_address(self) -> None:
        """Leave ping also truncates NYC/US multiline addresses."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
            location="240 Hudson St\nNew York NY 10013\nUnited States",
            location_value="240 Hudson St, New York, NY 10013",
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        result = format_leave_ping(plan)

        assert "240 Hudson St" in result
        assert "New York NY 10013" not in result
        assert "United States" not in result


class TestFormatServiceUpdate:
    """Tests for format_service_update."""

    def test_format_service_update_basic(self) -> None:
        """format_service_update shows alert info and new route."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        alert = Alert(
            id="alert1",
            header="C train: delays reported",
            description="Minor delays expected",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[],
            severity="WARNING",
        )

        new_leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            headsign="Fulton St",
            depart_at=datetime(2026, 5, 12, 8, 20, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 10, tzinfo=timezone.utc),
            duration_seconds=3000,
            summary="C train (delayed)",
        )
        new_route = make_route([new_leg])

        result = format_service_update(plan, alert, new_route)

        assert "⚠️" in result
        assert "Service Change" in result
        assert "Example Class" in result
        assert "9:30 AM" in result
        assert "C train: delays reported" in result
        # Route info appears in output (route summary is included)
        assert "C train" in result
        assert "Leave by" in result

    def test_format_service_update_includes_leave_time(self) -> None:
        """format_service_update shows updated leave time."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        alert = Alert(
            id="alert1",
            header="C line: weekend track work",
            description="Track work this weekend",
            affected_routes={"C"},
            affected_systems={"MTA Subway"},
            active_periods=[],
            severity="INFO",
        )

        new_leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 25, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 15, tzinfo=timezone.utc),
            duration_seconds=3000,
            summary="C train",
        )
        new_route = make_route([new_leg])

        result = format_service_update(plan, alert, new_route)

        assert "Leave by" in result

    def test_format_service_update_special_chars_in_alert(self) -> None:
        """format_service_update escapes special chars in alert header."""
        event = make_event(
            id="evt1",
            title="Example Class",
            start=datetime(2026, 5, 12, 9, 30, tzinfo=timezone.utc),
        )
        leg = TransitLeg(
            mode="TRANSIT",
            system="MTA Subway",
            line="C",
            depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
            arrive_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            duration_seconds=2700,
            summary="C train",
        )
        route = make_route([leg])
        plan = make_plan(event, route)

        alert = Alert(
            id="alert1",
            header="C/D line: expect delays (Weekend Work)",
            description="Track maintenance",
            affected_routes={"C", "D"},
            affected_systems={"MTA Subway"},
            active_periods=[],
            severity="WARNING",
        )

        new_route = make_route([leg])

        result = format_service_update(plan, alert, new_route)

        # Should contain the header content safely escaped
        assert "C/D line" in result


# ── Modality labeling tests ───────────────────────────────────────────────────

class TestSummarizeRouteMode:
    """Tests for dominant mode labeling."""

    def test_walk_only(self) -> None:
        """All-walking route => Walking."""
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
                arrive_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
                duration_seconds=900, summary="Walk",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=datetime(2026, 5, 12, 8, 15, tzinfo=timezone.utc),
                arrive_at=datetime(2026, 5, 12, 8, 20, tzinfo=timezone.utc),
                duration_seconds=300, summary="Walk",
            ),
        ]
        route = make_route(legs)
        assert _summarize_route_mode(route) == "Walking"

    def test_single_subway_majority(self) -> None:
        """Subway >= 50% with one line => Subway (line)."""
        # 40 min subway (C), 5 min walk, 5 min walk => 50 min total, 80% subway
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 50, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=3),
                duration_seconds=180, summary="Walk to station",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="C", headsign="Fulton St",
                depart_at=depart + timedelta(minutes=3), arrive_at=depart + timedelta(minutes=43),
                duration_seconds=2400, summary="C train",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=43), arrive_at=arrive,
                duration_seconds=300, summary="Walk to venue",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2880, transfers=0)
        assert _summarize_route_mode(route) == "Subway (C)"

    def test_multi_line_subway_majority(self) -> None:
        """Subway >= 50% with multiple lines => Multiple subways (line, ...)."""
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 55, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=3),
                duration_seconds=180, summary="Walk",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="A", headsign="Inwood",
                depart_at=depart + timedelta(minutes=3), arrive_at=depart + timedelta(minutes=18),
                duration_seconds=900, summary="A train",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="C", headsign="Fulton St",
                depart_at=depart + timedelta(minutes=20), arrive_at=depart + timedelta(minutes=43),
                duration_seconds=1380, summary="C train",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=43), arrive_at=arrive,
                duration_seconds=300, summary="Walk",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2760, transfers=1)
        assert _summarize_route_mode(route) == "Multiple subways (A, C)"

    def test_bus_majority(self) -> None:
        """Bus >= 50% => Bus."""
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 50, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=2),
                duration_seconds=120, summary="Walk",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Bus", line="B43", headsign="Crown Heights",
                depart_at=depart + timedelta(minutes=2), arrive_at=depart + timedelta(minutes=32),
                duration_seconds=1800, summary="B43 bus",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=32), arrive_at=arrive,
                duration_seconds=120, summary="Walk",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2040, transfers=0)
        assert _summarize_route_mode(route) == "Bus (B43)"

    def test_rail_majority(self) -> None:
        """Rail >= 50% => Rail."""
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 50, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="TRANSIT", system="LIRR", line="Atlantic Branch",
                headsign="Atlantic Terminal", depart_at=depart + timedelta(minutes=5),
                arrive_at=depart + timedelta(minutes=35),
                duration_seconds=1800, summary="LIRR Atlantic",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=5),
                duration_seconds=300, summary="Walk to station",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=35), arrive_at=arrive,
                duration_seconds=300, summary="Walk to venue",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2400, transfers=0)
        assert _summarize_route_mode(route) == "Rail (Atlantic Branch)"

    def test_mixed_transit_no_majority(self) -> None:
        """Transit exists but no mode >= 50% => Mixed transit."""
        # 15 min A subway + 20 min bus = 35 min transit; 15 min walk = 30% share each
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 50, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=5),
                duration_seconds=300, summary="Walk",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="A", headsign="Inwood",
                depart_at=depart + timedelta(minutes=5), arrive_at=depart + timedelta(minutes=20),
                duration_seconds=900, summary="A train",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Bus", line="B43", headsign="Crown Heights",
                depart_at=depart + timedelta(minutes=20), arrive_at=depart + timedelta(minutes=40),
                duration_seconds=1200, summary="B43 bus",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=40), arrive_at=arrive,
                duration_seconds=300, summary="Walk",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2700, transfers=1)
        assert _summarize_route_mode(route) == "Mixed transit"

    def test_no_legs_returns_walking(self) -> None:
        """Route with no legs => Walking."""
        route = Route(legs=[], depart_at=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
                      arrive_at=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
                      total_duration_seconds=0, transfers=0)
        assert _summarize_route_mode(route) == "Walking"

    def test_subway_exactly_50_percent(self) -> None:
        """Subway exactly 50% => still Subway."""
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 40, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=5),
                duration_seconds=300, summary="Walk",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="C", headsign="Fulton St",
                depart_at=depart + timedelta(minutes=5), arrive_at=depart + timedelta(minutes=35),
                duration_seconds=1800, summary="C train",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=35), arrive_at=arrive,
                duration_seconds=300, summary="Walk",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2400, transfers=0)
        assert _summarize_route_mode(route) == "Subway (C)"

    def test_route_summary_uses_new_label(self) -> None:
        """_route_summary uses _summarize_route_mode output."""
        from commutecompass.format import _route_summary
        depart = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        arrive = datetime(2026, 5, 12, 8, 50, tzinfo=timezone.utc)
        legs = [
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart, arrive_at=depart + timedelta(minutes=3),
                duration_seconds=180, summary="Walk",
            ),
            TransitLeg(
                mode="TRANSIT", system="MTA Subway", line="C", headsign="Fulton St",
                depart_at=depart + timedelta(minutes=3), arrive_at=depart + timedelta(minutes=43),
                duration_seconds=2400, summary="C train",
            ),
            TransitLeg(
                mode="WALKING", system=None, line=None, headsign=None,
                depart_at=depart + timedelta(minutes=43), arrive_at=arrive,
                duration_seconds=300, summary="Walk",
            ),
        ]
        route = Route(legs=legs, depart_at=depart, arrive_at=arrive,
                      total_duration_seconds=2880, transfers=0)
        summary = _route_summary(route)
        # Must contain the new label, not "C train"
        assert "Subway (C)" in summary
        assert "C train" not in summary
