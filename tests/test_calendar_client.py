"""Tests for calendar_client module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from commutecompass.models import CalendarSpec
from commutecompass.calendar_client import AuthError, CalendarClient
from commutecompass.timeutil import NYC_TZ


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_aware(dt: datetime) -> datetime:
    """Make a datetime timezone-aware (UTC)."""
    return dt.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client_secret_json() -> str:
    """Return a valid OAuth client secret JSON string."""
    return json.dumps({
        "installed": {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    })


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """Return a path within a temporary directory for token storage."""
    return tmp_path / "google_token.json"


@pytest.fixture
def calendar_specs() -> list[CalendarSpec]:
    """Return a list of CalendarSpec objects for testing."""
    return [
        CalendarSpec(id="cal-theatre", name="Theatre", enabled=True),
        CalendarSpec(id="cal-school", name="School", enabled=True),
        CalendarSpec(id="cal-personal", name="Personal", enabled=False),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Token persistence tests
# ─────────────────────────────────────────────────────────────────────────────

def test_token_not_found_raises_auth_error(client_secret_json: str, token_path: Path) -> None:
    """Loading credentials when token file is missing raises AuthError."""
    client = CalendarClient(client_secret_json, token_path)
    with pytest.raises(AuthError, match="Token not found"):
        client._load_credentials()


def test_saved_credentials_have_restricted_permissions(
    client_secret_json: str, token_path: Path
) -> None:
    """Saved tokens should have mode 0600."""
    client = CalendarClient(client_secret_json, token_path)
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = json.dumps({
        "token": "ya29.test",
        "refresh_token": "refresh-token-value",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
    })

    with patch("commutecompass.calendar_client.InstalledAppFlow") as mock_flow_class:
        mock_flow_instance = MagicMock()
        mock_flow_instance.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_config.return_value = mock_flow_instance
        client.authorize_interactive()

    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600


# ─────────────────────────────────────────────────────────────────────────────
# authorize_interactive
# ─────────────────────────────────────────────────────────────────────────────

def test_authorize_interactive_saves_token(
    client_secret_json: str, token_path: Path
) -> None:
    """authorize_interactive should save a usable token to disk."""
    client = CalendarClient(client_secret_json, token_path)

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = json.dumps({
        "token": "ya29.a0AfH6SMBx",
        "refresh_token": "refresh-token-value",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
    })

    with patch("commutecompass.calendar_client.InstalledAppFlow") as mock_flow_class:
        mock_flow_instance = MagicMock()
        mock_flow_instance.run_local_server.return_value = mock_creds
        mock_flow_class.from_client_config.return_value = mock_flow_instance

        client.authorize_interactive()

    mock_flow_instance.run_local_server.assert_called_once_with(port=0)
    assert token_path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# fetch_events — cancelled / all-day skip behavior
# ─────────────────────────────────────────────────────────────────────────────

def _build_mock_service(items: list[dict[str, Any]]) -> MagicMock:
    """Build a fully-mocked Google Calendar service."""
    service = MagicMock()

    # Shared events list response
    events_list_mock = MagicMock()
    events_list_mock.execute.return_value = {"items": items, "nextPageToken": None}
    events_list_mock.list.return_value = events_list_mock

    service.events.return_value.list.return_value = events_list_mock
    service.events.return_value.list.return_value.execute.return_value = {
        "items": items,
        "nextPageToken": None,
    }

    return service


def test_fetch_events_skips_cancelled(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """Cancelled events must be excluded from results."""
    items = [
        {
            "id": "event-active",
            "status": "confirmed",
            "summary": "Active Rehearsal",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T12:00:00+00:00"},
        },
        {
            "id": "event-cancelled",
            "status": "cancelled",
            "summary": "Cancelled Rehearsal",
            "start": {"dateTime": "2026-05-08T14:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T16:00:00+00:00"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-theatre", name="Theatre", enabled=True)],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 1
    assert events[0].id == "event-active"
    assert events[0].title == "Active Rehearsal"


def test_fetch_events_skips_all_day(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """Events with no dateTime (all-day) must be excluded from results."""
    items = [
        {
            "id": "event-timed",
            "status": "confirmed",
            "summary": "Morning Class",
            "start": {"dateTime": "2026-05-08T09:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T11:00:00+00:00"},
        },
        {
            "id": "event-all-day",
            "status": "confirmed",
            "summary": "All-Day Rehearsal",
            "start": {"date": "2026-05-08"},
            "end": {"date": "2026-05-09"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-theatre", name="Theatre", enabled=True)],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 1
    assert events[0].id == "event-timed"


def test_fetch_events_skips_disabled_calendars(
    client_secret_json: str,
    token_path: Path,
    calendar_specs: list[CalendarSpec],
) -> None:
    """Disabled calendars must be skipped."""
    items = [
        {
            "id": "event-from-disabled",
            "status": "confirmed",
            "summary": "Should Not Appear",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T12:00:00+00:00"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            calendar_specs,
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    # calendar_specs[2] is disabled (Personal). Only Theatre and School are enabled.
    # The mock returns the same items regardless of calendarId, so we just verify
    # that events were fetched (calendar_id will match first enabled calendar in list).
    assert all(e.calendar_id in ("cal-theatre", "cal-school") for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# fetch_events — event mapping
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_events_maps_all_fields(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """All fields of an Event should be populated from the API response."""
    items = [
        {
            "id": "event-1",
            "status": "confirmed",
            "summary": "Example Class",
            "location": "200 Example St, New York, NY 10001",
            "start": {"dateTime": "2026-05-08T09:30:00+00:00"},
            "end": {"dateTime": "2026-05-08T12:00:00+00:00"},
        },
    ]

    cal_specs = [CalendarSpec(id="theatre-calendar@example.com", name="Theatre")]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            cal_specs,
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 1
    event = events[0]
    assert event.id == "event-1"
    assert event.calendar_id == "theatre-calendar@example.com"
    assert event.calendar_name == "Theatre"
    assert event.title == "Example Class"
    assert event.location_raw == "200 Example St, New York, NY 10001"
    assert event.location_resolved is None
    assert event.mode_override is None


def test_fetch_events_defaults_title_when_missing(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """Events without a summary should get '(No title)' as title."""
    items = [
        {
            "id": "event-no-title",
            "status": "confirmed",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T11:00:00+00:00"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 1
    assert events[0].title == "(No title)"


def test_fetch_events_handles_pagination(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """Multiple pages of events should all be collected."""
    page1 = [
        {
            "id": f"page1-event-{i}",
            "status": "confirmed",
            "summary": f"Event {i}",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T11:00:00+00:00"},
        }
        for i in range(3)
    ]
    page2 = [
        {
            "id": f"page2-event-{i}",
            "status": "confirmed",
            "summary": f"Event {i+3}",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T11:00:00+00:00"},
        }
        for i in range(2)
    ]

    service = MagicMock()

    def list_side_effect(**kwargs: Any) -> Any:
        req = MagicMock()
        if kwargs.get("pageToken") == "token-page-2":
            req.execute.return_value = {"items": page2, "nextPageToken": None}
        else:
            req.execute.return_value = {
                "items": page1,
                "nextPageToken": "token-page-2",
            }
        return req

    service.events.return_value.list.side_effect = list_side_effect

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=service):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 5
    ids = {e.id for e in events}
    assert "page1-event-0" in ids
    assert "page2-event-0" in ids


def test_fetch_events_empty_response(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """An empty items list should return an empty events list."""
    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service([])):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert events == []


# ─────────────────────────────────────────────────────────────────────────────
# AuthError semantics
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_error_raises_auth_error(client_secret_json: str, token_path: Path) -> None:
    """A RefreshError from google-auth should be wrapped as AuthError."""
    from google.auth.exceptions import RefreshError as GoogleRefreshError

    # Write an expired token to disk so _load_credentials attempts refresh
    token_data = json.dumps({
        "token": "expired-token",
        "refresh_token": "refresh-token",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "expiry": "1970-01-01T00:00:00Z",
    })
    token_path.write_text(token_data)

    client = CalendarClient(client_secret_json, token_path)

    with patch("commutecompass.calendar_client.Credentials.from_authorized_user_info") as mock_from_info:
        mock_creds = MagicMock()
        mock_creds.expired = True
        mock_from_info.return_value = mock_creds

        with patch.object(mock_creds, "refresh", side_effect=GoogleRefreshError("Token expired")):  # type: ignore[no-untyped-call]
            with pytest.raises(AuthError, match="Token refresh failed"):
                client._load_credentials()


# ─────────────────────────────────────────────────────────────────────────────
# Multiple calendar support
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_events_queries_multiple_calendars(client_secret_json: str, token_path: Path) -> None:
    """Each enabled calendar should be queried separately."""
    cal_theatre = CalendarSpec(id="theatre-cal", name="Theatre", enabled=True)
    cal_school = CalendarSpec(id="school-cal", name="School", enabled=True)

    theatre_event = {
        "id": "theatre-event",
        "status": "confirmed",
        "summary": "Rehearsal",
        "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
        "end": {"dateTime": "2026-05-08T12:00:00+00:00"},
    }
    school_event = {
        "id": "school-event",
        "status": "confirmed",
        "summary": "Class",
        "start": {"dateTime": "2026-05-08T14:00:00+00:00"},
        "end": {"dateTime": "2026-05-08T15:00:00+00:00"},
    }

    service = MagicMock()

    # Return different events depending on which calendar was requested
    def list_side_effect(**kwargs: Any) -> Any:
        calendar_id = kwargs.get("calendarId", "")
        req = MagicMock()
        if calendar_id == "theatre-cal":
            req.execute.return_value = {"items": [theatre_event], "nextPageToken": None}
        elif calendar_id == "school-cal":
            req.execute.return_value = {"items": [school_event], "nextPageToken": None}
        else:
            req.execute.return_value = {"items": [], "nextPageToken": None}
        return req

    service.events.return_value.list.side_effect = list_side_effect

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=service):
        events = client.fetch_events(
            [cal_theatre, cal_school],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 2
    ids = {e.id for e in events}
    assert "theatre-event" in ids
    assert "school-event" in ids


def test_fetch_events_location_raw_optional(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """Events without a location field should have location_raw = None."""
    items = [
        {
            "id": "event-no-location",
            "status": "confirmed",
            "summary": "Private Meeting",
            "start": {"dateTime": "2026-05-08T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-08T11:00:00+00:00"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 8, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 8, 23, 59, 59)),
        )

    assert len(events) == 1
    assert events[0].location_raw is None


# ─────────────────────────────────────────────────────────────────────────────
# Timezone conversion tests
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_events_converts_explicit_offset_to_nyc(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """An event with an explicit offset in dateTime must be converted to NYC wall time.

    2026-05-09T14:00:00+01:00  →  09:00 America/New_York (EDT, UTC-4)
    """
    items = [
        {
            "id": "event-offset",
            "status": "confirmed",
            "summary": "Offset Event",
            "start": {"dateTime": "2026-05-09T14:00:00+01:00"},
            "end": {"dateTime": "2026-05-09T16:00:00+01:00"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 9, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 9, 23, 59, 59)),
        )

    assert len(events) == 1
    # 14:00+01:00 == 13:00 UTC; in NYC (EDT, UTC-4) that is 09:00
    assert events[0].start == datetime(2026, 5, 9, 9, 0, tzinfo=NYC_TZ)
    assert events[0].end == datetime(2026, 5, 9, 11, 0, tzinfo=NYC_TZ)


def test_fetch_events_converts_naive_datetime_with_timezone_field_to_nyc(
    client_secret_json: str,
    token_path: Path,
) -> None:
    """A naive dateTime with a separate timeZone field must be converted to NYC.

    dateTime=14:00, timeZone=Europe/London (BST=UTC+1 in May)
      → 13:00 UTC → 09:00 America/New_York (EDT, UTC-4)
    """
    items = [
        {
            "id": "event-tz-field",
            "status": "confirmed",
            "summary": "TZ Field Event",
            "start": {"dateTime": "2026-05-09T14:00:00", "timeZone": "Europe/London"},
            "end": {"dateTime": "2026-05-09T16:00:00", "timeZone": "Europe/London"},
        },
    ]

    client = CalendarClient(client_secret_json, token_path)

    with patch.object(client, "_build_service", return_value=_build_mock_service(items)):
        events = client.fetch_events(
            [CalendarSpec(id="cal-1", name="Test")],
            start=make_aware(datetime(2026, 5, 9, 0, 0, 0)),
            end=make_aware(datetime(2026, 5, 9, 23, 59, 59)),
        )

    assert len(events) == 1
    # 14:00 BST (UTC+1) = 13:00 UTC = 09:00 EDT in NYC
    assert events[0].start == datetime(2026, 5, 9, 9, 0, tzinfo=NYC_TZ)
    assert events[0].end == datetime(2026, 5, 9, 11, 0, tzinfo=NYC_TZ)