"""Message formatters."""

from __future__ import annotations

from commutecop.models import Alert, Plan


def escape_md(s: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    # Characters that need escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r"\_*[]()~`>#+\-=|{}.!"
    for c in escape_chars:
        s = s.replace(c, f"\\{c}")
    return s


def format_digest(plans: list[Plan], alerts: list[Alert]) -> str:
    """Format the daily digest message."""
    raise NotImplementedError()


def format_prep_ping(plan: Plan) -> str:
    """Format a 'start getting ready' ping."""
    raise NotImplementedError()


def format_leave_ping(plan: Plan) -> str:
    """Format a 'leave now' ping."""
    raise NotImplementedError()


def format_service_update(plan: Plan, alert: Alert, new_route: "Route") -> str:  # type: ignore[name-defined]
    """Format a service change update."""
    raise NotImplementedError()