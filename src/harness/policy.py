"""Policy checks owned by the AutoBrowser harness."""

from __future__ import annotations

from typing import Any

from src.agent.state import AgentState, PolicyDecision, ToolRequest
from src.browser import (
    is_browser_snapshot_name,
    is_browser_tool_name,
    to_canonical_browser_name,
)
from src.agent.subgraphs.observer.utils import has_invalid_ref_text
from src.harness.memory import append_tool_message

BLOCKED_TOOL_MARKERS = (
    "payment",
    "purchase",
    "delete_account",
    "credential",
)
SNAPSHOT_REUSE_MARKERS = (
    "browser.snapshot is already current",
    "browser_snapshot is already current",
)
SNAPSHOT_REUSE_MARKER = SNAPSHOT_REUSE_MARKERS[0]


def _snapshot_reuse_was_blocked(state: AgentState) -> bool:
    policy_event = state.get("policy_event") or {}
    reason = str(policy_event.get("reason", "") or "")
    observation = str(state.get("observation", "") or "")
    error = str(state.get("error", "") or "")
    payload = "\n".join([reason, observation, error]).lower()
    return any(marker in payload for marker in SNAPSHOT_REUSE_MARKERS)


class PolicyEngine:
    """Harness-facing policy boundary for tool execution decisions."""

    def classify_tool_request(
        self,
        state: AgentState,
        request: ToolRequest | None,
    ) -> tuple[PolicyDecision, str]:
        """Classify whether a tool call may execute automatically."""

        return classify_tool_request(state, request)

    def node(self, state: AgentState) -> dict[str, Any]:
        """Apply policy to the selected tool request."""

        decision, reason = self.classify_tool_request(state, state.get("tool_request"))
        return _policy_updates(state, decision, reason)


def classify_tool_request(
    state: AgentState,
    request: ToolRequest | None,
) -> tuple[PolicyDecision, str]:
    """Classify whether a tool call may execute automatically."""

    if not request or not request.get("name"):
        return "blocked", "No tool request was provided."

    requested_name = str(request["name"]).strip()
    name = requested_name.lower()
    canonical_name = to_canonical_browser_name(name) if is_browser_tool_name(name) else name
    if any(marker in canonical_name for marker in BLOCKED_TOOL_MARKERS):
        return "needs_human", f"Tool requires human approval before use: {requested_name}"

    ineffective_action_count = int(state.get("ineffective_action_count", 0) or 0)
    if ineffective_action_count >= 3:
        return (
            "blocked",
            "The last browser actions repeatedly did not change the visible page. "
            "Replan with a different control, a direct URL fallback, or finish with "
            "the visible result instead of continuing UI retries.",
        )

    if is_browser_snapshot_name(canonical_name):
        needs_fresh_snapshot = bool(state.get("needs_fresh_snapshot"))
        has_active_invalid_ref = has_invalid_ref_text(state.get("error", ""))
        has_current_snapshot = bool(str(state.get("snapshot", "") or "").strip())
        requested_args = request.get("args") or {}
        last_snapshot_args = (
            state.get("last_args", {})
            if is_browser_snapshot_name(str(state.get("last_tool", "")))
            else {}
        )
        is_same_snapshot_request = requested_args == last_snapshot_args
        if (
            has_current_snapshot
            and not needs_fresh_snapshot
            and not has_active_invalid_ref
            and (is_same_snapshot_request or _snapshot_reuse_was_blocked(state))
        ):
            return (
                "blocked",
                "browser.snapshot is already current. Reuse the existing snapshot "
                "and refs instead of requesting another snapshot with varied depth. "
                "Use browser_find or browser.evaluate only if the visible structure "
                "is insufficient, or replan.",
            )

    display_name = canonical_name if is_browser_tool_name(name) else requested_name
    return "approved", f"Tool approved: {display_name}"


def policy_node(state: AgentState) -> dict[str, Any]:
    """Apply policy to the selected tool request."""

    decision, reason = classify_tool_request(state, state.get("tool_request"))
    return _policy_updates(state, decision, reason)


def _policy_updates(
    state: AgentState,
    decision: PolicyDecision,
    reason: str,
) -> dict[str, Any]:
    """Build state updates for a policy decision."""

    updates: dict[str, Any] = {
        "policy_decision": decision,
        "observation": reason,
        "policy_event": {
            "decision": decision,
            "reason": reason,
            "tool_request": state.get("tool_request") or {},
        },
    }
    if decision == "blocked":
        updates["error"] = reason
        updates["consecutive_failures"] = (
            int(state.get("consecutive_failures", 0) or 0) + 1
        )
        request = state.get("tool_request") or {}
        updates["messages"] = append_tool_message(
            list(state.get("messages") or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        )
    elif decision == "needs_human":
        updates["error"] = ""
    return updates


__all__ = [
    "BLOCKED_TOOL_MARKERS",
    "PolicyEngine",
    "classify_tool_request",
    "policy_node",
]
