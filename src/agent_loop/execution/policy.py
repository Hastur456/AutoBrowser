"""Engine-native policy classification for tool execution.

Ported from ``src/harness/policy.py``. Classification rules (blocked-tool markers →
``needs_human``; a call that already returned the identical result
``settings.loop.max_ineffective_actions`` times (action journal, any tool) → ``blocked``;
snapshot-reuse → ``blocked``; otherwise ``approved``), plus the block-side
effect (``consecutive_failures += 1``, a tool message, and the ``policy_event``). State
access is typed :class:`~src.agent_loop.execution.state.LoopState` attribute reads; the
returned flat update dict is applied through :meth:`LoopState.apply`.

Server-neutral: no element-ref or server-specific error-text parsing and no canonical
tool-name mapping — tool names are compared exactly as the MCP bridge exposes them. The few
names the loop itself issues (``browser_snapshot``, ``browser_tabs``) are defined here once.
Whether a snapshot must be refreshed is decided only by ``browser.needs_fresh_snapshot``
(set by the observation compiler, e.g. when the browser MCP server was restarted).
"""

from __future__ import annotations

from typing import Any

from src.config import get_settings
from src.contracts import PolicyDecision, ToolRequest
from src.harness.memory import append_tool_message

from src.agent_loop.execution.progress import identical_outcome_count
from src.agent_loop.execution.state import LoopState

# Tool names the native loop relies on: the browser server's own names, exposed unprefixed
# by the MCP bridge (``MCPToolSource(unprefixed_servers=[browser server])``).
BROWSER_TOOL_PREFIX = "browser_"
SNAPSHOT_TOOL = "browser_snapshot"
TABS_TOOL = "browser_tabs"


def is_browser_tool(name: Any) -> bool:
    """True for tools of the browser server (``browser_*``)."""

    return str(name or "").startswith(BROWSER_TOOL_PREFIX)


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
    if any(marker in name for marker in BLOCKED_TOOL_MARKERS):
        return "needs_human", f"Tool requires human approval before use: {requested_name}"

    identical = identical_outcome_count(
        state.action_history,
        requested_name,
        request.get("args") or {},
    )
    if identical >= get_settings().loop.max_ineffective_actions:
        return (
            "blocked",
            f"Not executed: {requested_name} with these exact arguments already returned "
            f"the identical result {identical} times in this task (see Action History). "
            "Running it again cannot produce new information. Change the approach or the "
            "evidence you rely on, or finish with what is known.",
        )

    if name == SNAPSHOT_TOOL:
        needs_fresh_snapshot = bool(state.browser.needs_fresh_snapshot)
        has_current_snapshot = bool(str(state.browser.snapshot or "").strip())
        requested_args = request.get("args") or {}
        last_snapshot_args = (
            state.last_args
            if str(state.last_tool or "") == SNAPSHOT_TOOL
            else {}
        )
        is_same_snapshot_request = requested_args == last_snapshot_args
        if (
            has_current_snapshot
            and not needs_fresh_snapshot
            and (is_same_snapshot_request or _snapshot_reuse_was_blocked(state))
        ):
            return (
                "blocked",
                "browser.snapshot is already current. Reuse the existing snapshot "
                "instead of requesting another snapshot with varied depth. "
                "Use browser_find or browser.evaluate only if the visible structure "
                "is insufficient, or replan.",
            )

    return "approved", f"Tool approved: {requested_name}"


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
    "BROWSER_TOOL_PREFIX",
    "SNAPSHOT_TOOL",
    "TABS_TOOL",
    "is_browser_tool",
    "SNAPSHOT_REUSE_MARKER",
    "SNAPSHOT_REUSE_MARKERS",
    "classify_tool_request",
    "policy_updates",
]