"""Observation translation for executor results."""

from __future__ import annotations

import re
from typing import Any

from src.harness.memory import append_tool_message, tool_result_message_content
from src.browser.adapters import element_description_from_snapshot
from src.agent.subgraphs.observer.observer_llm import (
    compress_tool_result,
    fallback_compact_observation,
)
from src.agent.state import AgentState, CompactToolObservation
from .utils import (
    BROWSER_ACTION_TOOLS,
    extract_element_refs,
    _needs_fresh_snapshot_after_error,
    _observation_lines,
    _plan_completion_update,
    pending_tab_activation_from_result,
    request_ref_value,
    snapshot_contains_ref,
)


MAX_UNCHANGED_SNAPSHOTS = 3


def _snapshot_fingerprint(snapshot: str) -> str:
    lines = []
    for line in snapshot.splitlines():
        normalized = re.sub(r"\s+\[(?:active|focused)\]", "", line.strip())
        normalized = re.sub(r"\[ref=[^\]]+\]", "[ref]", normalized)
        normalized = re.sub(r"\bref=[A-Za-z][A-Za-z0-9_-]*\b", "ref", normalized)
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _action_identity(
    request: dict[str, Any],
    tool_name: str,
    snapshot: str = "",
) -> dict[str, Any]:
    args = dict(request.get("args") or {})
    action: dict[str, Any] = {
        "name": tool_name,
        "args": args,
    }
    ref = str(args.get("ref") or args.get("target") or "")
    if ref and snapshot:
        action["target_description"] = element_description_from_snapshot(snapshot, ref)
    return action


def _same_action(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("name") == right.get("name") and left.get("args", {}) == right.get(
        "args", {}
    )


def _needs_fresh_snapshot_after_timeout(
    state: AgentState,
    result: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    tool_name = str(result.get("name", "") or "")
    payload = str(result.get("error", "") or result.get("content", "") or "").lower()
    if tool_name not in BROWSER_ACTION_TOOLS or "timeout" not in payload:
        return False

    requested_ref = request_ref_value(request)
    if not requested_ref:
        return False

    snapshot = str(state.get("snapshot", "") or "")
    return not snapshot.strip() or not snapshot_contains_ref(snapshot, requested_ref)


def compile_observation(
    state: AgentState,
    compact_observation: CompactToolObservation | None = None,
    *,
    compress_tool_output: bool = False,
) -> dict[str, Any]:
    """Translate the latest ToolResult into a plain observation string."""

    result = state.get("tool_result") or {}
    tool_name = str(result.get("name", "") or "")
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    compact = compact_observation or fallback_compact_observation(result)

    is_browser_tool = tool_name.startswith("browser_")
    is_browser_action = tool_name in BROWSER_ACTION_TOOLS
    is_snapshot = tool_name == "browser_snapshot" and status == "success"
    request = state.get("tool_request") or {}
    timed_out_stale_ref = status == "error" and _needs_fresh_snapshot_after_timeout(
        state,
        result,
        request,
    )
    needs_fresh_after_error = status == "error" and (
        _needs_fresh_snapshot_after_error(result) or timed_out_stale_ref
    )
    refs = extract_element_refs(content) if is_snapshot else []
    observation_lines = _observation_lines(
        result, compact, refs, compress=compress_tool_output
    )
    if timed_out_stale_ref:
        observation_lines.append(
            "The ref-based action timed out without a current matching ref in "
            "browser_snapshot; take a fresh browser_snapshot before the next "
            "ref-based action."
        )
    pending_tab_activation = pending_tab_activation_from_result(result)
    if pending_tab_activation:
        _, pending_tab_reason = pending_tab_activation
        observation_lines.append(pending_tab_reason)
    messages = list(state.get("messages") or [])

    updates: dict[str, Any] = {
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
    }

    if is_snapshot:
        prior_unchanged_snapshots = int(state.get("unchanged_snapshot_count", 0) or 0)
        previous_browser_snapshot = str(state.get("snapshot", "") or "")
        current_fingerprint = _snapshot_fingerprint(content)
        previous_browser_fingerprint = _snapshot_fingerprint(previous_browser_snapshot)

        if previous_browser_fingerprint and current_fingerprint == previous_browser_fingerprint:
            unchanged_snapshot_count = prior_unchanged_snapshots + 1
        else:
            unchanged_snapshot_count = 1

        updates["snapshot"] = content
        updates["needs_fresh_snapshot"] = False
        updates["unchanged_snapshot_count"] = unchanged_snapshot_count
        previous_snapshot = str(state.get("snapshot_before_last_browser_action", "") or "")
        last_action = state.get("last_browser_action") or {}
        if previous_snapshot and last_action:
            previous_fingerprint = _snapshot_fingerprint(previous_snapshot)
            if current_fingerprint == previous_fingerprint:
                prior_ineffective = state.get("ineffective_browser_action") or {}
                prior_count = int(state.get("ineffective_action_count", 0) or 0)
                prior_history = list(state.get("ineffective_browser_actions") or [])
                updates["ineffective_browser_action"] = last_action
                updates["ineffective_browser_actions"] = [
                    *prior_history,
                    last_action,
                ][-5:]
                updates["ineffective_action_count"] = (
                    prior_count + 1 if _same_action(prior_ineffective, last_action) else 1
                )
                observation_lines.append(
                    "The last browser action did not change the visible snapshot. "
                    "Do not repeat the same action with the same target; try a "
                    "different visible control, a deeper snapshot, or a fallback route."
                )
            else:
                updates["ineffective_browser_action"] = {}
                updates["ineffective_browser_actions"] = []
                updates["ineffective_action_count"] = 0
        updates["snapshot_before_last_browser_action"] = ""
    elif is_browser_tool and (status == "success" or needs_fresh_after_error):
        if status == "success":
            if tool_name == "browser_tabs":
                updates["pending_browser_tab_index"] = 0
                updates["pending_browser_tab_reason"] = ""
            if is_browser_action:
                action_snapshot = str(state.get("snapshot", "") or "")
                updates["snapshot_before_last_browser_action"] = action_snapshot
                updates["last_browser_action"] = _action_identity(
                    request,
                    tool_name,
                    action_snapshot,
                )
            updates["snapshot"] = ""
            updates["unchanged_snapshot_count"] = 0
            if pending_tab_activation:
                pending_tab_index, pending_tab_reason = pending_tab_activation
                updates["pending_browser_tab_index"] = pending_tab_index
                updates["pending_browser_tab_reason"] = pending_tab_reason
        if needs_fresh_after_error:
            updates["snapshot"] = ""
            updates["needs_fresh_snapshot"] = True
            updates["unchanged_snapshot_count"] = 0

    observation = "\n\n".join(observation_lines)
    updates["observation"] = observation
    tool_message = tool_result_message_content(
        result,
        compact,
        refs,
        observation,
        compress=compress_tool_output,
    )
    updates["messages"] = append_tool_message(messages, request, tool_message)

    if status == "success":
        plan = state.get("plan") or []
        current_step = int(state.get("current_step", 0) or 0)
        plan_update = _plan_completion_update(
            plan,
            current_step,
            compact,
            tool_name=tool_name,
        )
        updates.update(plan_update)
        if plan_update:
            updates["steps_without_plan_advance"] = 0
        elif plan and current_step < len(plan):
            updates["steps_without_plan_advance"] = (
                int(state.get("steps_without_plan_advance", 0) or 0) + 1
            )
        updates["error"] = ""
        updates["consecutive_failures"] = 0
        if tool_name != "browser_snapshot":
            updates["stale_snapshot_retries"] = 0
            updates["invalid_ref_recovery_count"] = 0
        if (
            is_snapshot
            and int(updates.get("unchanged_snapshot_count", 0) or 0)
            >= MAX_UNCHANGED_SNAPSHOTS
        ):
            final_answer = (
                "Stopped because browser_snapshot returned the same visible state "
                "three consecutive times. Latest observation:\n\n"
                f"{observation}"
            )
            updates["decision"] = "done"
            updates["final_answer"] = final_answer
            updates["observation"] = final_answer
    else:
        updates["error"] = str(result.get("error", "") or "")
        updates["consecutive_failures"] = (
            int(state.get("consecutive_failures", 0) or 0) + 1
        )

    return updates


def observe_node(state: AgentState) -> dict[str, Any]:
    """Compile executor output into MCP-aware observation state without an LLM."""

    return compile_observation(state)


def create_observe_node(
    observer_llm: Any | None = None,
    *,
    compress_tools: bool = False,
) -> Any:
    """Create an observer node whose LLM only sees the latest ToolResult."""

    async def _observe_node(state: AgentState) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        compact_observation = None
        if compress_tools:
            compact_observation = await compress_tool_result(result, observer_llm)
        return compile_observation(
            state,
            compact_observation,
            compress_tool_output=compress_tools,
        )

    return _observe_node
