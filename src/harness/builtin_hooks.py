"""Generic, server-neutral lifecycle hook handlers shipped with AutoBrowser.

Each public function here is a **factory**: register it in ``settings.hooks.registry`` with
``options`` (``handler(**options)`` is called once at session start) and it returns the async
:data:`~src.contracts.HookHandler`. Browser-specific handlers live in
:mod:`src.browser.hooks`.
"""

from __future__ import annotations

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


__all__ = [
    "approve_tools",
]
