"""Message formatters."""

from __future__ import annotations

import re
from datetime import datetime

from commutecompass.models import Alert, Plan, Route


def _fmt_time(dt: datetime) -> str:
    """Render a datetime as 12-hour wall-clock time without a leading zero.

    Built explicitly because ``%-I`` is a GNU strftime extension: it raises
    on macOS and Windows and silently leaves the literal ``-I`` on some
    libcs.  Avoiding it keeps the formatter portable across dev machines.
    """
    hour12 = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {suffix}"


def _fmt_day_of_month(dt: datetime) -> str:
    """Render ``dt.day`` with no leading zero.  ``%-d`` is also a GNU extension."""
    return str(dt.day)


# Human-readable rendering for ``Plan.error`` codes.  Anything not listed here
# falls through to the raw error string (escaped) — so adding a new code never
# breaks rendering, it just shows the code until we add a friendly label.
_PLAN_ERROR_LABELS: dict[str, str] = {
    "location_unresolved": "Could not figure out the location",
    "no_route": "No transit route found",
    "too_imminent": "Event too imminent — leave now, no prep window",
}


def _plan_error_label(code: str) -> str:
    """Map a plan-error code to a human-readable label, escaped for MarkdownV2."""
    return escape_md(_PLAN_ERROR_LABELS.get(code, code))

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


# Unicode bidirectional control characters — strip from user-supplied strings
# so a stray RTL override in an event title cannot mangle the rendered message.
_BIDI_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩"
# Default maximum length for a sanitized field.  Long enough for realistic
# event titles, short enough to keep digests legible and well under Telegram's
# 4096-char message limit.
_SANITIZE_MAX_LEN = 200


def _sanitize_text(s: str | None, *, max_len: int = _SANITIZE_MAX_LEN) -> str:
    """Strip control chars / bidi overrides and truncate, before MarkdownV2 escape.

    Calendar feeds occasionally produce titles with embedded NULs, ANSI escapes
    (when copy-pasted from terminals), or Unicode bidi overrides that flip the
    visual order of the entire message.  None of those add information; all of
    them can break MarkdownV2 parsing.  Newlines (\\n, \\r) are normalised to
    spaces so a multi-line title doesn't collapse the digest layout.
    """
    if s is None:
        return ""
    # Drop bidi controls outright.
    cleaned = s.translate({ord(c): None for c in _BIDI_CONTROLS})
    # Strip other C0 / C1 control chars except tab (rare but harmless); convert
    # any embedded newline to a single space so the field stays on one line.
    out_chars: list[str] = []
    for ch in cleaned:
        codepoint = ord(ch)
        if ch in ("\n", "\r"):
            out_chars.append(" ")
        elif ch == "\t":
            out_chars.append(ch)
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            continue  # other control char — drop
        else:
            out_chars.append(ch)
    cleaned = "".join(out_chars)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def escape_md(s: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    MarkdownV2 special chars: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    # Build escape_chars explicitly to avoid backslash interpretation issues
    _SPECIAL = "_*[]()~`>#+-=|{}.!"
    for c in _SPECIAL:
        s = s.replace(c, f"\\{c}")
    return s


def _is_us_country_line(line: str) -> bool:
    """Return True when a line clearly denotes the United States."""
    normalized = line.strip().casefold().replace(".", "")
    return normalized in {
        "united states",
        "united states of america",
        "usa",
        "us",
    }


def _is_new_york_line(line: str) -> bool:
    """Return True when a line appears to be New York city/state location text."""
    normalized = line.strip().casefold().replace(",", " ")
    normalized = " ".join(normalized.split())
    return "new york" in normalized and " ny" in f" {normalized}"


def _compact_location(location_raw: str | None, *, fallback: str) -> str:
    """Compact multiline locations for cleaner message output.

    For standard NYC + United States addresses, show just the street line.
    Otherwise, flatten multiline addresses onto a single comma-separated line.
    """
    if not location_raw:
        return fallback

    lines = [line.strip() for line in location_raw.splitlines() if line.strip()]
    if not lines:
        return fallback
    if len(lines) == 1:
        return lines[0]

    first_line = lines[0]
    second_line = lines[1] if len(lines) >= 2 else ""
    last_line = lines[-1]

    if _is_new_york_line(second_line) and _is_us_country_line(last_line):
        return first_line

    return ", ".join(lines)


def format_digest(
    plans: list[Plan],
    alerts: list[Alert],
    *,
    operations_notes: list[str] | None = None,
) -> str:
    """Format the daily digest message.

    Args:
        plans: All plans for today.
        alerts: Any alerts affecting today's routes.
        operations_notes: Optional list of degraded-service messages to append
            as an "Operations" footer (e.g. "MTA Subway alerts unavailable").
            Each note is sanitised+escaped.  An empty list (or None) hides
            the footer entirely.

    Returns:
        Formatted digest string ready for Telegram.
    """
    from commutecompass.timeutil import now_nyc

    today = now_nyc()
    date_str = f"{today.strftime('%A, %b')} {_fmt_day_of_month(today)}"
    lines = [f"*Today — {date_str}*\n"]

    if not plans:
        # Keep this MarkdownV2-safe ('.' must be escaped otherwise)
        lines.append("No events scheduled for today")
    else:
        for plan in plans:
            lines.append(_format_plan_summary(plan))
        # Footer hint so OpenClaw / the user knows how to refer to an event in
        # follow-up commands like `adjust`, `mute`, `snooze`.
        lines.append(escape_md(
            "Refer to an event by today:N, or by 'next'."
        ))

    if alerts:
        lines.append("")
        lines.append(format_alerts_block(alerts))

    if operations_notes:
        lines.append("")
        lines.append("*Operations:*")
        for note in operations_notes:
            lines.append(f"⚙️ {escape_md(_sanitize_text(note))}")

    return "\n".join(lines)


def format_alerts_block(alerts: list[Alert]) -> str:
    """Render the "Active service alerts" block.

    Extracted from ``format_digest`` so the ``mta-alerts`` CLI command can
    reuse the exact same rendering (severity marker, route list) instead of
    re-inventing it.
    """
    lines = ["*Active service alerts:*"]
    for alert in alerts:
        marker = "🔴" if alert.severity == "SEVERE" else "⚠️"
        lines.append(f"{marker} {escape_md(_sanitize_text(alert.header))}")
        if alert.affected_routes:
            routes_str = ", ".join(sorted(alert.affected_routes))
            lines.append(f"  Routes: {escape_md(routes_str)}")
    return "\n".join(lines)


def short_event_id(event_id: str) -> str:
    """Return the 8-char short form used in the digest's `[id]` token.

    Stable across the day (Google Calendar event IDs are immutable for a
    given event), and accepted directly by ``selector.resolve_event_selector``
    via its hex-prefix path.
    """
    return event_id[:8]


def _format_plan_summary(plan: Plan) -> str:
    """Format a single plan summary block for the digest."""
    event = plan.event
    start_str = event.start.strftime("%I:%M %p").lstrip("0")

    lines = []
    cal_lower = event.calendar_name.lower()
    if "theatre" in cal_lower or "stage" in cal_lower:
        icon = "🎭"
    elif "job" in cal_lower or "work" in cal_lower:
        icon = "🍨"
    elif "school" in cal_lower or "class" in cal_lower:
        icon = "🎓"
    elif "personal" in cal_lower:
        icon = "📅"
    else:
        icon = "📍"
    lines.append(f"{icon} *{escape_md(_sanitize_text(event.title))}* \\({escape_md(_sanitize_text(event.calendar_name))}\\)")
    location_fallback = "Salt & Straw" if ("job" in cal_lower or "work" in cal_lower) else "(no location)"
    location = _compact_location(event.location_raw, fallback=location_fallback)
    if ("job" in cal_lower or "work" in cal_lower) and location.strip() == "(no location)":
        location = "Salt & Straw"
    lines.append(
        f"  {start_str} at {escape_md(_sanitize_text(location))}"
    )

    if plan.error:
        lines.append(f"  ❌ {_plan_error_label(plan.error)}")
        lines.append("")
        return "\n".join(lines)

    if plan.prep_at and plan.leave_at:
        prep_str = plan.prep_at.strftime("%I:%M %p").lstrip("0")
        leave_str = plan.leave_at.strftime("%I:%M %p").lstrip("0")
        lines.append(f"  Start prep: {prep_str} · Leave: {leave_str}")

    if plan.route:
        lines.append(f"  {escape_md(_route_summary(plan.route))}")

    if plan.weather_buffer_minutes > 0 and plan.weather_reason:
        emoji = "❄️" if plan.weather_reason == "snow" else "🌧️"
        lines.append(
            f"  {emoji} {escape_md(f'+{plan.weather_buffer_minutes} min for {plan.weather_reason}')}"
        )

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

    # Include transfer count only when there are transfers.
    xfers = route.transfers
    if xfers == 1:
        transfer_suffix = ", 1 transfer"
    elif xfers > 1:
        transfer_suffix = f", {xfers} transfers"
    else:
        transfer_suffix = ""

    # Mark routes that came from cache/estimate rather than live routing.
    estimate_suffix = ", estimated" if route.approximate else ""

    return f"{mode_label} ({total_min} min{transfer_suffix}{estimate_suffix})"


def _route_summary_detailed(route: Route) -> str:
    """Build a more detailed route summary for leave pings with actual times."""
    if not route.legs:
        return ""

    lines = []
    for leg in route.legs:
        if leg.mode == "TRANSIT" and leg.system:
            line = leg.line or ""
            depart = leg.depart_at.strftime("%I:%M %p").lstrip("0")
            arrive = leg.arrive_at.strftime("%I:%M %p").lstrip("0")
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
        title = _sanitize_text(plan.event.title)
        return (
            f"⏰ *Start getting ready*\n"
            f"{escape_md(title)}\n"
            f"⚠️ {_plan_error_label(plan.error)}"
        )

    leave_str = ""
    if plan.leave_at:
        leave_str = plan.leave_at.strftime("%I:%M %p").lstrip("0")

    title = escape_md(_sanitize_text(plan.event.title))
    start_str = plan.event.start.strftime("%I:%M %p").lstrip("0")

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
        title = _sanitize_text(plan.event.title)
        return f"🚶 *Leave now*\n{escape_md(title)}\n⚠️ {_plan_error_label(plan.error)}"

    title = escape_md(_sanitize_text(plan.event.title))
    start_str = plan.event.start.strftime("%I:%M %p").lstrip("0")
    location = escape_md(_sanitize_text(_compact_location(plan.event.location_raw, fallback="unknown location")))

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


def format_location_update(old_plan: Plan, new_plan: Plan) -> str:
    """Format a location-driven replan message.

    Args:
        old_plan: The plan as it stood before the replan.
        new_plan: The freshly computed plan from the user's current location.

    Returns:
        MarkdownV2-safe Telegram message.
    """
    title = escape_md(_sanitize_text(new_plan.event.title))
    start_str = new_plan.event.start.strftime("%I:%M %p").lstrip("0")

    lines = [
        "📍 *Location update*",
        f"{title} at {start_str}",
    ]

    if new_plan.error:
        lines.append(f"⚠️ {_plan_error_label(new_plan.error)}")
        return "\n".join(lines)

    if old_plan.leave_at and new_plan.leave_at:
        old_str = old_plan.leave_at.strftime("%I:%M %p").lstrip("0")
        new_str = new_plan.leave_at.strftime("%I:%M %p").lstrip("0")
        if old_str != new_str:
            lines.append(f"Leave by {new_str} \\(was {old_str}\\)")
        else:
            lines.append(f"Leave by {new_str}")
    elif new_plan.leave_at:
        lines.append(f"Leave by {new_plan.leave_at.strftime('%I:%M %p').lstrip('0')}")

    if new_plan.route:
        lines.append(escape_md(_route_summary(new_plan.route)))

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
    title = escape_md(_sanitize_text(plan.event.title))
    header = escape_md(_sanitize_text(alert.header))
    severity = "🔴" if alert.severity == "SEVERE" else "⚠️"

    lines = [
        f"{severity} *Service Change*",
        f"{title} at {plan.event.start.strftime('%I:%M %p').lstrip('0')}",
        f"{header}",
    ]

    if new_route.legs:
        lines.append(escape_md(_route_summary(new_route)))

    if plan.leave_at:
        lines.append(f"Leave by {plan.leave_at.strftime('%I:%M %p').lstrip('0')}")

    return "\n".join(lines)
