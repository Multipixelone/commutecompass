"""Morning digest job."""

from commutecop.models import Config


def run(config: Config) -> None:
    """Run the morning digest job.

    Sequence:
    1. Compute today's time window
    2. Fetch calendar events
    3. Plan each event
    4. Persist plans and schedule pings
    5. Pull MTA alerts affecting today's routes
    6. Send digest via Telegram
    """
    raise NotImplementedError()