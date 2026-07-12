"""Policy checks for tool execution."""

from __future__ import annotations

from typing import Any

from src.subgraphs.observer.utils import has_invalid_ref_text

from src.agent.history import append_tool_message
from src.agent.state import AgentState, PolicyDecision, ToolRequest

BLOCKED_TOOL_MARKERS = (
    "payment",
    "purchase",
    "delete_account",
    "credential",
)


def classify_tool_request(
    state: AgentState,
    request: ToolRequest | None,
) -> tuple[PolicyDecision, str]:
    """Classify whether a tool call may execute automatically."""

    if not request or not request.get("name"):
        return "blocked", "No tool request was provided."

    name = request["name"].lower()
    if any(marker in name for marker in BLOCKED_TOOL_MARKERS):
        return "blocked", f"Tool requires a stronger policy before use: {request['name']}"

    if name == "browser_snapshot":
        needs_fresh_snapshot = bool(state.get("needs_fresh_snapshot"))
        has_active_invalid_ref = has_invalid_ref_text(state.get("error", ""))
        has_current_snapshot = bool(str(state.get("snapshot", "") or "").strip())
        if has_current_snapshot and not needs_fresh_snapshot and not has_active_invalid_ref:
            return (
                "blocked",
                "browser_snapshot is already current. Reuse the existing snapshot "
                "and refs instead of requesting another snapshot.",
            )

    return "approved", f"Tool approved: {request['name']}"


def policy_node(state: AgentState) -> dict[str, Any]:
    """Apply policy to the selected tool request."""

    decision, reason = classify_tool_request(state, state.get("tool_request"))
    updates: dict[str, Any] = {
        "policy_decision": decision,
        "observation": reason,
    }
    if decision == "blocked":
        updates["error"] = reason
        request = state.get("tool_request") or {}
        updates["messages"] = append_tool_message(
            list(state.get("messages") or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        )
    return updates
