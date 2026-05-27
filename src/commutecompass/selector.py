"""Resolve user-friendly event selectors to a calendar event_id.

Used by every chat-driven command that takes an event reference (``adjust``,
``plan``, ``snooze``, ``mute``, ``undo``).  OpenClaw rarely has the raw
Google Calendar event_id at hand — the digest now surfaces an 8-char prefix
plus an index, and the user typically refers to events by title.  This
helper accepts:

    - ``next``           → the earliest today-plan whose ``event.start`` > now
    - ``today:N``        → 1-indexed pick from ``store.today_plans()``
    - 8+ hex prefix      → unique prefix of an event_id present in today's plans
    - full event_id      → exact match against any today-plan
    - free-form title    → fuzzy match via rapidfuzz (score >= 72, margin >= 10)

On anything ambiguous, raises ``SelectorError`` carrying a CLI exit code so
the dispatch helper in ``cli.py`` can ``sys.exit`` with the right value.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commutecompass.store import Store


# Exit-code constants mirror those in ``cli.py``.  Keeping them as bare ints
# (rather than importing from cli) avoids a module-level import cycle.
_EXIT_NOT_FOUND = 65
_EXIT_UNRESOLVED = 66


# 8 hex chars matches the digest's short-ID token.  Allow any length >= 8 so
# the user can paste a longer prefix if it disambiguates.
_HEX_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")

# Fuzzy-match thresholds.  WRatio returns 0-100; require a comfortable margin
# over the runner-up to avoid silently picking the wrong event when two have
# similar titles.
_FUZZ_SCORE_CUTOFF = 72
_FUZZ_MARGIN = 10


class SelectorError(Exception):
    """Raised when a selector cannot be resolved unambiguously.

    ``exit_code`` is the sysexits-style code the CLI should exit with —
    ``EXIT_NOT_FOUND`` (65) when the selector matches nothing, or
    ``EXIT_UNRESOLVED`` (66) when it matches multiple candidates.
    """

    def __init__(self, exit_code: int, message: str) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def resolve_event_selector(
    selector: str, store: "Store", *, now: datetime
) -> str:
    """Map a user-friendly selector to a single Google Calendar event_id.

    Resolution order matches the docstring at the top of the module.  Each
    step inspects today's plans (``store.today_plans()``) so off-day refs
    naturally fail rather than mutating yesterday's state.
    """
    if not selector or not selector.strip():
        raise SelectorError(_EXIT_NOT_FOUND, "empty selector")
    sel = selector.strip()

    plans = store.today_plans()

    # 1. "next"
    if sel.lower() == "next":
        upcoming = [p for p in plans if p.event.start > now]
        if not upcoming:
            raise SelectorError(
                _EXIT_NOT_FOUND, "no upcoming events today after now"
            )
        return upcoming[0].event.id

    # 2. "today:N"
    if sel.lower().startswith("today:"):
        suffix = sel.split(":", 1)[1].strip()
        try:
            n = int(suffix)
        except ValueError as exc:
            raise SelectorError(
                _EXIT_NOT_FOUND,
                f"invalid today selector {sel!r}: index must be an integer",
            ) from exc
        if n < 1 or n > len(plans):
            raise SelectorError(
                _EXIT_NOT_FOUND,
                f"today:{n} is out of range (today has {len(plans)} plans)",
            )
        return plans[n - 1].event.id

    # 3. Hex-prefix match against today's plans.
    if _HEX_PREFIX_RE.match(sel):
        prefix = sel.lower()
        matches = [p.event.id for p in plans if p.event.id.lower().startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SelectorError(
                _EXIT_UNRESOLVED,
                f"{sel!r} matches multiple events: {', '.join(m[:8] for m in matches)}",
            )
        # No today-plan starts with this hex; fall through to the exact-id
        # path so callers can still target an old event by its full id.

    # 4. Exact full event_id match.
    exact = [p for p in plans if p.event.id == sel]
    if exact:
        return exact[0].event.id

    # 5. Fuzzy title match — only meaningful when we have plans to score against.
    # On ambiguous best-vs-runner-up, raise so the caller can prompt; on no
    # confident match, fall through to the raw-passthrough below so a
    # downstream "No plan found" gives the friendliest error.
    if plans:
        try:
            from rapidfuzz import fuzz, process
        except ImportError:  # pragma: no cover - dependency required
            return sel  # selector module degrades gracefully; downstream handles

        titles = [p.event.title for p in plans]
        scored = process.extract(
            sel,
            titles,
            scorer=fuzz.WRatio,
            score_cutoff=_FUZZ_SCORE_CUTOFF,
            limit=3,
        )
        if scored:
            best_title, best_score, best_idx = scored[0]
            runner_score = scored[1][1] if len(scored) > 1 else 0.0
            if best_score - runner_score >= _FUZZ_MARGIN:
                return plans[best_idx].event.id
            candidates = ", ".join(f"{t!r}" for t, _, _ in scored)
            raise SelectorError(
                _EXIT_UNRESOLVED,
                f"{sel!r} ambiguous between: {candidates}",
            )

    # 6. Nothing matched.  Hand the input back to the caller; the downstream
    # command (e.g. ``adjust``) will produce a clearer "No plan found" message
    # tied to its own error surface.
    return sel
