"""Generic, server-neutral lifecycle hook handlers shipped with AutoBrowser.

Each public function here is a **factory**: register it in ``settings.hooks.registry`` with
``options`` (``handler(**options)`` is called once at session start) and it returns the async
:data:`~src.contracts.HookHandler`. Browser-specific handlers live in
:mod:`src.browser.hooks`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.contracts import HookEvent, HookHandler, HookResult


def approve_tools(tools: Sequence[str] = (), servers: Sequence[str] = ()) -> HookHandler:
    """``permission_request`` handler that pre-approves the listed tools or servers.

    Meant for batch/eval profiles where no human is at the keyboard: a ``needs_human`` call to
    a listed tool (exact exposed name) or to any tool of a listed MCP server runs without the
    human callback. Everything else gets no opinion, so it still goes to the human.
    """

    approved_tools = frozenset(str(name) for name in tools)
    approved_servers = frozenset(str(name) for name in servers)
    if not approved_tools and not approved_servers:
        raise ValueError("approve_tools needs at least one tool or server to approve.")

    async def handler(event: HookEvent) -> HookResult | None:
        if event.tool in approved_tools:
            return HookResult(decision="allow", reason=f"Pre-approved tool: {event.tool}")
        if event.server and event.server in approved_servers:
            return HookResult(decision="allow", reason=f"Pre-approved server: {event.server}")
        return None

    return handler


#: Thin spaces that may group digits: NBSP, narrow NBSP, thin space (plus a plain space).
_DIGIT_GROUP_SPACES = " \u00a0\u202f\u2009"

#: A number, possibly with space-grouped thousands (``1 299``) and a ``.``/``,`` fraction.
#: The look-behind keeps digits glued to letters (element refs like ``e10``) out.
_NUMBER = re.compile(
    rf"(?<![\w.,])(?:\d{{1,3}}(?:[{_DIGIT_GROUP_SPACES}]\d{{3}})+|\d+)(?:[.,]\d+)?"
)

#: Enumeration markers (``1.``/``2)`` at a line start) are layout, not claimed values.
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)](?=\s)")


def _normalize_number(text: str) -> str:
    """``1 299`` -> ``1299``, ``12,50`` -> ``12.5``, ``1299.00`` -> ``1299``."""

    value = "".join(char for char in text if char not in _DIGIT_GROUP_SPACES).replace(",", ".")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def _numbers(text: str) -> dict[str, str]:
    """``{normalized: first spelling}`` of the numbers in ``text``."""

    found: dict[str, str] = {}
    for match in _NUMBER.finditer(text):
        found.setdefault(_normalize_number(match.group()), match.group())
    return found


def grounded_final_answer(min_chars: int = 1) -> HookHandler:
    """``stop`` handler: reject a final answer that is empty or states unobserved numbers.

    Deterministic and deliberately narrow. A ``done`` is denied when the answer is shorter
    than ``min_chars`` or when a number in it (a price, a count) appears in none of
    ``event.evidence`` — the latest observation and browser snapshot. Digit grouping with
    spaces/NBSP is ignored (``1 299 ₽`` matches ``1299``) and ``,``/``.`` fractions are equal.
    Numbers that already occur in the task (``find 3 jackets``) and list markers (``1.``) are
    not checked.
    """

    if min_chars < 0:
        raise ValueError("min_chars must be >= 0.")

    async def handler(event: HookEvent) -> HookResult | None:
        answer = event.final_answer.strip()
        if len(answer) < max(min_chars, 1):
            return HookResult(
                decision="deny",
                reason=(
                    "The final answer is empty or too short. State the result the task asked "
                    "for, based on what the page shows."
                ),
            )

        claimed = _numbers(_LIST_MARKER.sub("", answer))
        observed = _numbers("\n".join(event.evidence))
        from_task = _numbers(event.task)
        unconfirmed = [
            spelling
            for value, spelling in claimed.items()
            if value not in observed and value not in from_task
        ]
        if not unconfirmed:
            return None
        return HookResult(
            decision="deny",
            reason=(
                "The final answer states values that the latest page observation does not "
                f"show: {', '.join(unconfirmed)}. Check the page again and report only values "
                "it shows."
            ),
        )

    return handler


__all__ = [
    "approve_tools",
    "grounded_final_answer",
]
