"""Engine-native built-in tool authorization (transitional).

Only the name-marker check is left here (blocked-tool markers → ``needs_human``; otherwise
``approved``). The progress check (an identical outcome repeated
``settings.loop.max_ineffective_actions`` times) lives in
:func:`~src.agent_loop.execution.guards.progress_block_reason`, and every block goes through
:func:`~src.agent_loop.execution.guards.tool_block_updates`. Browser tool names live in
:mod:`src.browser.names`.
"""

from __future__ import annotations

from typing import Any

from src.contracts import PolicyDecision, ToolRequest

from src.agent_loop.execution.guards import tool_block_updates
from src.agent_loop.execution.state import LoopState

BLOCKED_TOOL_MARKERS = (
    "payment",
    "purchase",
    "delete_account",
    "credential",
)


def classify_tool_request(
    state: LoopState,
    request: ToolRequest | None,
) -> tuple[PolicyDecision, str]:
    """Classify whether a tool call may execute automatically."""

    requested_name = str((request or {}).get("name") or "").strip()
    name = requested_name.lower()
    if any(marker in name for marker in BLOCKED_TOOL_MARKERS):
        return "needs_human", f"Tool requires human approval before use: {requested_name}"
    return "approved", f"Tool approved: {requested_name}"


def policy_updates(
    state: LoopState,
    decision: PolicyDecision,
    reason: str,
) -> dict[str, Any]:
    """Build state updates for a policy decision."""

    if decision == "blocked":
        return tool_block_updates(state, reason)
    updates: dict[str, Any] = {
        "policy_decision": decision,
        "observation": reason,
        "policy_event": {
            "decision": decision,
            "reason": reason,
            "tool_request": state.tool_request or {},
        },
    }
    if decision == "needs_human":
        updates["error"] = ""
    return updates


__all__ = [
    "BLOCKED_TOOL_MARKERS",
    "classify_tool_request",
    "policy_updates",
]
