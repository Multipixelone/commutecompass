"""CLI entry point."""

import click


@click.group()
def main() -> None:
    """commutecop — NYC commute orchestrator."""
    pass


@main.command()
def oauth() -> None:
    """Interactive Google Calendar OAuth setup."""
    click.echo("OAuth setup not yet implemented.")


@main.command()
def init_db() -> None:
    """Initialize SQLite database schema."""
    click.echo("init-db not yet implemented.")


@main.command()
def morning() -> None:
    """Run morning digest job."""
    click.echo("morning job not yet implemented.")


@main.command()
def poll() -> None:
    """Run poll loop job."""
    click.echo("poll job not yet implemented.")


@main.command()
@click.argument("event_id")
def plan(event_id: str) -> None:
    """Replan a single event (debug)."""
    click.echo(f"plan {event_id} not yet implemented.")


@main.command()
def test_notify() -> None:
    """Send a test Telegram message."""
    click.echo("test-notify not yet implemented.")


@main.command()
def bot() -> None:
    """Telegram bot mode (stub)."""
    click.echo("Telegram bot mode is not yet implemented.")


if __name__ == "__main__":
    main()