"""Google Calendar client with OAuth authentication."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from commutecompass.models import CalendarSpec, Event
from commutecompass.timeutil import to_nyc

if TYPE_CHECKING:
    import googleapiclient.discovery  # type: ignore[import-untyped]


class AuthError(Exception):
    """Raised when OAuth refresh fails and user needs to re-authenticate."""


class CalendarFetchError(Exception):
    """Raised when a calendar page cannot be safely treated as a complete result."""


_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)


def _is_virtual_only(item: dict[str, Any]) -> bool:
    """True if an event is a video call with no physical location.

    Google Meet attachments arrive as ``hangoutLink``/``conferenceData`` while
    ``location`` stays empty; other providers often paste the join URL into
    ``location``.  Either way there is nowhere to travel to (the event is still
    planned, with a get-ready time but no commute).  An event with a
    real (non-URL) location is kept even if it also has a call attached.
    """
    location = (item.get("location") or "").strip()
    location_is_url = bool(location) and all(
        _URL_RE.match(part.strip()) for part in re.split(r"[,;\n]", location) if part.strip()
    )
    if location and not location_is_url:
        return False
    has_conference = bool(item.get("hangoutLink") or item.get("conferenceData"))
    return location_is_url or has_conference



# Scopes for read-only calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarClient:
    """Google Calendar API client with OAuth and event fetching."""

    def __init__(self, client_secret_json: str, token_path: Path | str) -> None:
        self.client_secret_json = client_secret_json
        self.token_path = Path(token_path)
        self._creds: Credentials | None = None

    def _load_credentials(self) -> Credentials:
        """Load credentials from token file, if they exist."""
        if not self.token_path.exists():
            raise AuthError(
                f"Token not found at {self.token_path}. "
                "Run `commutecompass oauth` first."
            )

        try:
            creds_data = json.loads(self.token_path.read_text())
            if not isinstance(creds_data, dict):
                raise TypeError("token must be a JSON object")
            creds = cast(
                Credentials,
                Credentials.from_authorized_user_info(creds_data, SCOPES),  # type: ignore[no-untyped-call]
            )
        except (OSError, ValueError, TypeError) as exc:
            raise AuthError(
                "Token file is unreadable or invalid. Please re-run `commutecompass oauth`."
            ) from exc

        if creds.expired:
            try:
                from google.auth.transport.requests import Request

                creds.refresh(Request())  # type: ignore[no-untyped-call]
                self._save_credentials(creds)
            except RefreshError as exc:
                raise AuthError(
                    "Token refresh failed. Please re-run `commutecompass oauth`."
                ) from exc

        return creds

    def _save_credentials(self, creds: Credentials) -> None:
        """Atomically replace credentials using a private, synced temporary file."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        creds_data = creds.to_json()  # type: ignore[no-untyped-call]
        temp_path: Path | None = None
        try:
            # NamedTemporaryFile creates mode 0600 from the outset, even with a
            # permissive umask. Same-directory replacement stays atomic.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.token_path.parent,
                prefix=f".{self.token_path.name}.", delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                stream.write(creds_data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.token_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def authorize_interactive(self) -> None:
        """For first-run `commutecompass oauth` — opens browser OAuth flow."""
        # client_secret_json is the raw JSON string content, not a file path
        client_config = json.loads(self.client_secret_json)

        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)

        self._save_credentials(creds)
        self._creds = creds

    def _build_service(self) -> googleapiclient.discovery.Resource:
        """Build the Google Calendar API service object."""
        from googleapiclient import discovery

        creds = self._load_credentials()
        return discovery.build("calendar", "v3", credentials=creds)

    @staticmethod
    def _parse_event_datetime(dt_str: str, tz_name: str | None = None) -> datetime:
        """Parse Google Calendar datetime and normalize to NYC timezone.

        Google may provide a timezone-aware ISO string (with Z/offset), or a
        naive datetime paired with a separate ``timeZone`` field.
        """
        parsed = datetime.fromisoformat(dt_str)
        if parsed.tzinfo is None:
            if tz_name:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            else:
                # Defensive fallback for malformed payloads with no tz info.
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return to_nyc(parsed)

    def fetch_events(
        self,
        calendars: list[CalendarSpec],
        start: datetime,
        end: datetime,
    ) -> list[Event]:
        """Fetch events from the specified calendars in the time window.

        Args:
            calendars: List of CalendarSpec objects to fetch from.
            start: Start of time window (aware datetime).
            end: End of time window (aware datetime).

        Returns:
            List of Event objects mapped from the Google Calendar API response.

        Raises:
            AuthError: If token refresh fails.
        """
        service = self._build_service()
        events: list[Event] = []

        for cal in calendars:
            if not cal.enabled:
                continue

            page_token: str | None = None
            while True:
                response = (
                    service.events()
                    .list(
                        calendarId=cal.id,
                        timeMin=start.isoformat(),
                        timeMax=end.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        pageToken=page_token,
                    )
                    .execute()
                )

                # A broken page is a failed fetch, not an authoritative empty
                # calendar (nor permission to return earlier partial results).
                if not isinstance(response, Mapping):
                    raise CalendarFetchError("Invalid calendar response: expected a mapping")
                items = response.get("items", [])
                if not isinstance(items, list):
                    raise CalendarFetchError("Invalid calendar response: expected an items list")

                for index, item in enumerate(items):
                    try:
                        # Skip cancelled events and valid all-day events.
                        if item.get("status") == "cancelled":
                            continue
                        event_id = item["id"]
                        if not isinstance(event_id, str) or not event_id.strip():
                            raise ValueError("missing event id")
                        start_info = item.get("start", {})
                        end_info = item.get("end", {})
                        if not start_info.get("dateTime") and start_info.get("date"):
                            continue

                        event = Event(
                            id=event_id,
                            calendar_id=cal.id,
                            calendar_name=cal.name,
                            title=item.get("summary", "(No title)"),
                            start=self._parse_event_datetime(
                                start_info["dateTime"], start_info.get("timeZone")
                            ),
                            end=self._parse_event_datetime(
                                end_info["dateTime"], end_info.get("timeZone")
                            ),
                            location_raw=item.get("location"),
                            location_resolved=None,
                            mode_override=None,
                            virtual=_is_virtual_only(item),
                        )
                    except (KeyError, ValueError, TypeError, AttributeError):
                        # Never include payloads or exception text: they may contain
                        # private calendar titles, locations, or malformed values.
                        logging.getLogger(__name__).warning(
                            "Skipping malformed calendar event at index=%d", index
                        )
                        continue
                    events.append(event)

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return events
