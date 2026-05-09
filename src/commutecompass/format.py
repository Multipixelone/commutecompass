"""Message formatters."""

from __future__ import annotations

from commutecompass.models import Alert, Plan, Route


def escape_md(s: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    MarkdownV2 special chars: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    # Build escape_chars explicitly to avoid backslash interpretation issues
    _SPECIAL = "_*[]()~`>#+-=|{}.!"
    for c in _SPECIAL:
        s = s.replace(c, f"\\{c}")
    return s


def format_digest(plans: list[Plan], alerts: list[Alert]) -> str:
    """Format the daily digest message.

    Args:
        plans: All plans for today.
        alerts: Any alerts affecting today's routes.

    Returns:
        Formatted digest string ready for Telegram.
    """
    from commutecompass.timeutil import now_nyc

    today = now_nyc()
    date_str = today.strftime("%A, %b %-d").replace("  ", " ")
    lines = [f"*Today — {date_str}*\n"]

    if not plans:
        # Keep this MarkdownV2-safe ('.' must be escaped otherwise)
        lines.append("No events scheduled for today")
    else:
        for plan in plans:
            lines.append(_format_plan_summary(plan))

    if alerts:
        lines.append("")
        lines.append("*Active service alerts:*")
        for alert in alerts:
            marker = "🔴" if alert.severity == "SEVERE" else "⚠️"
            lines.append(f"{marker} {escape_md(alert.header)}")
            if alert.affected_routes:
                routes_str = ", ".join(sorted(alert.affected_routes))
                lines.append(f"  Routes: {escape_md(routes_str)}")

    return "\n".join(lines)


def _format_plan_summary(plan: Plan) -> str:
    """Format a single plan summary block for the digest."""
    event = plan.event
    start_str = event.start.strftime("%-I:%M %p")

    lines = []
    cal_lower = event.calendar_name.lower()
    if "theatre" in cal_lower or "stage" in cal_lower:
        icon = "🎭"
    elif "school" in cal_lower or "class" in cal_lower:
        icon = "🎓"
    elif "personal" in cal_lower:
        icon = "📅"
    else:
        icon = "📍"
    lines.append(f"{icon} *{escape_md(event.title)}* \\({escape_md(event.calendar_name)}\\)")
    lines.append(f"  {start_str} at {escape_md(event.location_raw or '(no location)')}")

    if plan.error:
        lines.append(f"  ❌ {escape_md(plan.error)}")
        lines.append("")
        return "\n".join(lines)

    if plan.prep_at and plan.leave_at:
        prep_str = plan.prep_at.strftime("%-I:%M %p")
        leave_str = plan.leave_at.strftime("%-I:%M %p")
        lines.append(f"  Start prep: {prep_str} · Leave: {leave_str}")

    if plan.route:
        lines.append(f"  {escape_md(_route_summary(plan.route))}")

    lines.append("")
    return "\n".join(lines)


def _route_summary(route: Route) -> str:
    """Build a one-line route summary string for digest."""
    if not route.legs:
        return "Route unavailable"

    total_min = route.total_duration_seconds // 60
    parts = []

    # First leg: origin mode + line
    first = route.legs[0]
    if first.mode == "TRANSIT" and first.system and first.line:
        line_name = f"{first.line} train" if first.system == "MTA Subway" else first.line
        parts.append(f"{line_name} from {first.system}")
    elif first.mode == "WALKING":
        parts.append("Walking")
    elif first.mode == "DRIVING":
        parts.append("Driving")

    # Last leg: destination
    last = route.legs[-1]
    if last.mode == "TRANSIT" and last.headsign:
        parts.append(f"→ {last.headsign}")

    parts.append(f"({total_min} min, {route.transfers} transfer{'s' if route.transfers != 1 else ''})")
    return " ".join(parts)


def _route_summary_detailed(route: Route) -> str:
    """Build a more detailed route summary for leave pings with actual times."""
    if not route.legs:
        return ""

    lines = []
    for leg in route.legs:
        if leg.mode == "TRANSIT" and leg.system:
            line = leg.line or ""
            depart = leg.depart_at.strftime("%-I:%M %p")
            arrive = leg.arrive_at.strftime("%-I:%M %p")
            if leg.system == "MTA Subway":
                lines.append(f"{line} train, {depart} → {arrive}")
            elif leg.system == "LIRR":
                branch = f" ({line})" if line else ""
                lines.append(f"LIRR{branch}, {depart} → {arrive}")
            else:
                lines.append(f"{leg.system} {line}, {depart} → {arrive}")
        elif leg.mode == "WALKING":
            mins = leg.duration_seconds // 60
            lines.append(f"Walk {mins} min")
    return " · ".join(lines)


def format_prep_ping(plan: Plan) -> str:
    """Format a 'start getting ready' ping.

    Args:
        plan: The plan for this event.

    Returns:
        Formatted prep ping string.
    """
    if plan.error:
        title = plan.event.title
        return (
            f"⏰ *Start getting ready*\n"
            f"{escape_md(title)}\n"
            f"⚠️ Could not compute route: {escape_md(plan.error)}"
        )

    leave_str = ""
    if plan.leave_at:
        leave_str = plan.leave_at.strftime("%-I:%M %p")

    title = escape_md(plan.event.title)
    start_str = plan.event.start.strftime("%-I:%M %p")

    lines = [
        f"⏰ *Start getting ready*",
        f"{title} at {start_str}",
    ]
    if leave_str:
        lines.append(f"Leave by {leave_str}")

    return "\n".join(lines)


def format_leave_ping(plan: Plan) -> str:
    """Format a 'leave now' ping.

    Args:
        plan: The plan for this event.

    Returns:
        Formatted leave ping string.
    """
    if plan.error:
        title = plan.event.title
        return f"🚶 *Leave now*\n{escape_md(title)}\n⚠️ {escape_md(plan.error)}"

    title = escape_md(plan.event.title)
    start_str = plan.event.start.strftime("%-I:%M %p")
    location = escape_md(plan.event.location_raw or "unknown location")

    lines = [
        f"🚶 *Leave now*",
        f"{title} at {start_str} — {location}",
    ]

    if plan.route:
        # Include route summary with first and last leg info
        route_info = _route_summary_detailed(plan.route)
        if route_info:
            lines.append(escape_md(route_info))

    return "\n".join(lines)


def format_service_update(plan: Plan, alert: Alert, new_route: Route) -> str:
    """Format a service change update.

    Args:
        plan: The original plan.
        alert: The alert causing the change.
        new_route: The newly computed route.

    Returns:
        Formatted service update string.
    """
    title = escape_md(plan.event.title)
    header = escape_md(alert.header)
    severity = "🔴" if alert.severity == "SEVERE" else "⚠️"

    lines = [
        f"{severity} *Service Change*",
        f"{title} at {plan.event.start.strftime('%-I:%M %p')}",
        f"{header}",
    ]

    if new_route.legs:
        lines.append(escape_md(_route_summary(new_route)))

    if plan.leave_at:
        lines.append(f"Leave by {plan.leave_at.strftime('%-I:%M %p')}")

    return "\n".join(lines)
