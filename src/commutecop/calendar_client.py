"""Google Calendar client."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from commutecop.models import CalendarSpec, Event


class AuthError(Exception):
    """Raised when OAuth fails and user needs to re-authenticate."""
    pass


class CalendarClient:
    """Google Calendar API client with OAuth."""

    def __init__(self, client_secret_json: str, token_path: Path) -> None:
        self.client_secret_json = client_secret_json
        self.token_path = token_path

    def authorize_interactive(self) -> None:
        """For first-run `commutecop oauth` — opens browser flow."""
        raise NotImplementedError()

    def fetch_events(
        self,
        calendars: list[CalendarSpec],
        start: datetime,
        end: datetime,
    ) -> list[Event]:
        """Fetch events from the specified calendars in the time window."""
        raise NotImplementedError()