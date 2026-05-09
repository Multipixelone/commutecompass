"""Message formatters."""

from __future__ import annotations

import re

from commutecompass.models import Alert, Plan, Route

# Tuning knob — raise to require stricter subway share before using subway label
SUBWAY_MAJORITY_THRESHOLD = 0.5

_SUBWAY_VERBOSE = re.compile(r"^([A-Za-z]{1,3})\s+Train", re.IGNORECASE)


def _normalize_line(line: str) -> str:
    """Extract concise line ID from a potentially verbose Google Directions line name.

    Google Directions sometimes returns verbose names like "C Train (8 Av Local)"
    or "B Bus (Crosstown)" instead of just the short route ID "C".  This tries to
    extract the shortest meaningful identifier.

    Strategy:
      - If the line matches the common pattern "X Train (desc)" or "X Bus (desc)",
        strip the description and return just the letter/number.
      - For LIRR/rail branch names, return the line unchanged (they're already short).
      - Subway lines are always a single letter + digit (e.g. "C", "G", "FS").
    """
    line = line.strip()
    if not line:
        return line

    # Verbose subway pattern: "C Train (8 Av Local)" → "C"
    m = _SUBWAY_VERBOSE.match(line)
    if m:
        return m.group(1)

    return line


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


def _summarize_route_mode(route: Route) -> str:
    """Compute the dominant mode label for a route.

    Aggregates duration by mode and subway line, then applies precedence:
      - no transit legs         => Walking
      - subway >= 50% share    => Subway (line) or Multiple subways (line, ...)
      - bus >= 50% share       => Bus
      - rail >= 50% share      => Rail
      - transit exists but no single mode >= 50% => Mixed transit
    """
    if not route.legs:
        return "Walking"

    total = route.total_duration_seconds
    if total == 0:
        return "Walking"

    subway_seconds = 0
    subway_lines: set[str] = set()
    bus_seconds = 0
    bus_lines: set[str] = set()
    rail_seconds = 0
    rail_lines: set[str] = set()
    transit_seconds = 0

    for leg in route.legs:
        dur = leg.duration_seconds
        if leg.mode == "WALKING":
            continue
        if leg.mode != "TRANSIT":
            # Driving / bicycling treated as its own category
            transit_seconds += dur
            continue

        transit_seconds += dur
        system = leg.system or ""
        raw_line = leg.line or ""
        line = _normalize_line(raw_line)

        if system == "MTA Subway":
            subway_seconds += dur
            if line:
                subway_lines.add(line)
        elif system == "MTA Bus":
            bus_seconds += dur
            if line:
                bus_lines.add(line)
        elif system in ("LIRR", "Rail"):
            rail_seconds += dur
            if line:
                rail_lines.add(line)
        else:
            # Generic transit — treat as rail for share purposes
            rail_seconds += dur
            if line:
                rail_lines.add(line)

    # No transit at all => Walking
    if transit_seconds == 0:
        return "Walking"

    subway_share = subway_seconds / total
    bus_share = bus_seconds / total
    rail_share = rail_seconds / total

    if subway_share >= SUBWAY_MAJORITY_THRESHOLD:
        if len(subway_lines) == 0:
            return "Subway"
        if len(subway_lines) == 1:
            return f"Subway ({next(iter(subway_lines))})"
        return f"Multiple subways ({', '.join(sorted(subway_lines))})"

    if bus_share >= 0.5:
        if len(bus_lines) == 0:
            return "Bus"
        if len(bus_lines) == 1:
            return f"Bus ({next(iter(bus_lines))})"
        return f"Multiple buses ({', '.join(sorted(bus_lines))})"

    if rail_share >= 0.5:
        if len(rail_lines) == 0:
            return "Rail"
        if len(rail_lines) == 1:
            return f"Rail ({next(iter(rail_lines))})"
        return f"Multiple rail lines ({', '.join(sorted(rail_lines))})"

    # Transit exists but no mode dominates
    return "Mixed transit"


def _route_summary(route: Route) -> str:
    """Build a one-line route summary string for digest."""
    if not route.legs:
        return "Route unavailable"

    total_min = route.total_duration_seconds // 60
    mode_label = _summarize_route_mode(route)

    # Transfer count suffix
    xfers = route.transfers
    if xfers == 0:
        transfer_str = "no transfers"
    elif xfers == 1:
        transfer_str = "1 transfer"
    else:
        transfer_str = f"{xfers} transfers"

    return f"{mode_label} ({total_min} min, {transfer_str})"


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
        "⏰ *Start getting ready*",
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
        "🚶 *Leave now*",
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
