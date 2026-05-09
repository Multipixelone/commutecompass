"""Google Calendar client with OAuth authentication."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from commutecompass.models import CalendarSpec, Event

if TYPE_CHECKING:
    import googleapiclient.discovery


class AuthError(Exception):
    """Raised when OAuth refresh fails and user needs to re-authenticate."""

    pass


# Scopes for read-only calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarClient:
    """Google Calendar API client with OAuth and event fetching."""

    def __init__(self, client_secret_json: str, token_path: Path | str) -> None:
        self.client_secret_json = client_secret_json
        self.token_path = Path(token_path)
        self._creds: Optional[Credentials] = None

    def _load_credentials(self) -> Credentials:
        """Load credentials from token file, if they exist."""
        if not self.token_path.exists():
            raise AuthError(
                f"Token not found at {self.token_path}. "
                "Run `commutecompass oauth` first."
            )

        creds_data = json.loads(self.token_path.read_text())
        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)

        if creds.expired:
            try:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
                self._save_credentials(creds)
            except RefreshError as exc:
                raise AuthError(
                    "Token refresh failed. Please re-run `commutecompass oauth`."
                ) from exc

        return creds

    def _save_credentials(self, creds: Credentials) -> None:
        """Persist credentials to token file with restricted permissions."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        creds_data = creds.to_json()
        self.token_path.write_text(creds_data)
        os.chmod(self.token_path, 0o600)

    def authorize_interactive(self) -> None:
        """For first-run `commutecompass oauth` — opens browser OAuth flow."""
        # client_secret_json is the raw JSON string content, not a file path
        client_config = json.loads(self.client_secret_json)

        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)

        self._save_credentials(creds)
        self._creds = creds

    def _build_service(self) -> "googleapiclient.discovery.Resource":
        """Build the Google Calendar API service object."""
        from googleapiclient import discovery

        creds = self._load_credentials()
        return discovery.build("calendar", "v3", credentials=creds)

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

            page_token: Optional[str] = None
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

                for item in response.get("items", []):
                    # Skip cancelled events
                    if item.get("status") == "cancelled":
                        continue

                    start_info = item.get("start", {})
                    end_info = item.get("end", {})

                    # Skip all-day events (no dateTime, only date)
                    if not start_info.get("dateTime"):
                        continue

                    event = Event(
                        id=item["id"],
                        calendar_id=cal.id,
                        calendar_name=cal.name,
                        title=item.get("summary", "(No title)"),
                        start=datetime.fromisoformat(
                            start_info["dateTime"].replace("Z", "+00:00")
                        ),
                        end=datetime.fromisoformat(
                            end_info["dateTime"].replace("Z", "+00:00")
                        ),
                        location_raw=item.get("location"),
                        location_resolved=None,
                        mode_override=None,
                    )
                    events.append(event)

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return events
