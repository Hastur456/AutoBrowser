"""Engine-native policy classification for tool execution.

Ported from ``src/harness/policy.py``. The classification rules and every reason string
are preserved verbatim (blocked-tool markers → ``needs_human``;
``ineffective_action_count >= 3`` → ``blocked``; snapshot-reuse → ``blocked``; otherwise
``approved``), plus the block-side effect (``consecutive_failures += 1``, a tool message,
and the ``policy_event``). Only the state access is rewritten to typed
:class:`~src.agent_loop.execution.state.LoopState` attribute reads; the returned flat
update dict is applied through :meth:`LoopState.apply`.
"""

from __future__ import annotations

from typing import Any

from src.contracts import PolicyDecision, ToolRequest
from src.browser.observation import has_invalid_ref_text
from src.browser import (
    is_browser_snapshot_name,
    is_browser_tool_name,
    to_canonical_browser_name,
)
from src.harness.memory import append_tool_message

from src.agent_loop.execution.state import LoopState

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


def _snapshot_reuse_was_blocked(state: LoopState) -> bool:
    policy_event = state.policy_event or {}
    reason = str(policy_event.get("reason", "") or "")
    observation = str(state.observation or "")
    error = str(state.error or "")
    payload = "\n".join([reason, observation, error]).lower()
    return any(marker in payload for marker in SNAPSHOT_REUSE_MARKERS)


def classify_tool_request(
    state: LoopState,
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

    ineffective_action_count = int(state.ineffective_action_count or 0)
    if ineffective_action_count >= 3:
        return (
            "blocked",
            "The last browser actions repeatedly did not change the visible page. "
            "Replan with a different control, a direct URL fallback, or finish with "
            "the visible result instead of continuing UI retries.",
        )

    if is_browser_snapshot_name(canonical_name):
        needs_fresh_snapshot = bool(state.browser.needs_fresh_snapshot)
        has_active_invalid_ref = has_invalid_ref_text(state.error)
        has_current_snapshot = bool(str(state.browser.snapshot or "").strip())
        requested_args = request.get("args") or {}
        last_snapshot_args = (
            state.last_args
            if is_browser_snapshot_name(str(state.last_tool))
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


def policy_updates(
    state: LoopState,
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
            "tool_request": state.tool_request or {},
        },
    }
    if decision == "blocked":
        updates["error"] = reason
        updates["consecutive_failures"] = (
            int(state.consecutive_failures or 0) + 1
        )
        request = state.tool_request or {}
        updates["messages"] = append_tool_message(
            list(state.messages or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        )
    elif decision == "needs_human":
        updates["error"] = ""
    return updates


__all__ = [
    "BLOCKED_TOOL_MARKERS",
    "SNAPSHOT_REUSE_MARKER",
    "SNAPSHOT_REUSE_MARKERS",
    "classify_tool_request",
    "policy_updates",
]
