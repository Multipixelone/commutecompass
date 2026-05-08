"""Poll loop job."""

from commutecop.models import Config


def run(config: Config) -> None:
    """Run the poll loop job.

    Sequence:
    1. Honor quiet hours
    2. Fire any due pings
    3. Fetch fresh MTA alerts
    4. Re-plan affected events and send service updates as needed
    """
    raise NotImplementedError()