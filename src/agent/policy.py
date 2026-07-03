"""Policy checks for tool execution."""

from __future__ import annotations

from typing import Any

from src.agent.state import AgentState, PolicyDecision, ToolRequest

BLOCKED_TOOL_MARKERS = (
    "payment",
    "purchase",
    "delete_account",
    "credential",
)


def classify_tool_request(request: ToolRequest | None) -> tuple[PolicyDecision, str]:
    """Classify whether a tool call may execute automatically."""

    if not request or not request.get("name"):
        return "blocked", "No tool request was provided."

    name = request["name"].lower()
    if any(marker in name for marker in BLOCKED_TOOL_MARKERS):
        return "blocked", f"Tool requires a stronger policy before use: {request['name']}"

    return "approved", f"Tool approved: {request['name']}"


def policy_node(state: AgentState) -> dict[str, Any]:
    """Apply policy to the selected tool request."""

    decision, reason = classify_tool_request(state.get("tool_request"))
    updates: dict[str, Any] = {
        "policy_decision": decision,
        "observation": reason,
    }
    if decision == "blocked":
        updates["error"] = reason
    return updates
