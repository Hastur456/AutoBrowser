"""Observation translation for executor results."""

from __future__ import annotations

import re
from typing import Any

from src.harness.memory import append_tool_message, tool_result_message_content
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
    _plan_completion_update
)


def _snapshot_fingerprint(snapshot: str) -> str:
    lines = []
    for line in snapshot.splitlines():
        normalized = re.sub(r"\s+\[(?:active|focused)\]", "", line.strip())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _action_identity(request: dict[str, Any], tool_name: str) -> dict[str, Any]:
    return {
        "name": tool_name,
        "args": dict(request.get("args") or {}),
    }


def _same_action(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("name") == right.get("name") and left.get("args", {}) == right.get(
        "args", {}
    )


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
    needs_fresh_after_error = status == "error" and _needs_fresh_snapshot_after_error(
        result
    )
    refs = extract_element_refs(content) if is_snapshot else []
    observation_lines = _observation_lines(
        result, compact, refs, compress=compress_tool_output
    )
    request = state.get("tool_request") or {}
    messages = list(state.get("messages") or [])

    updates: dict[str, Any] = {
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
    }

    if is_snapshot:
        updates["snapshot"] = content
        updates["needs_fresh_snapshot"] = False
        previous_snapshot = str(state.get("snapshot_before_last_browser_action", "") or "")
        last_action = state.get("last_browser_action") or {}
        if previous_snapshot and last_action:
            current_fingerprint = _snapshot_fingerprint(content)
            previous_fingerprint = _snapshot_fingerprint(previous_snapshot)
            if current_fingerprint == previous_fingerprint:
                prior_ineffective = state.get("ineffective_browser_action") or {}
                prior_count = int(state.get("ineffective_action_count", 0) or 0)
                updates["ineffective_browser_action"] = last_action
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
                updates["ineffective_action_count"] = 0
        updates["snapshot_before_last_browser_action"] = ""
    elif is_browser_tool and (status == "success" or needs_fresh_after_error):
        if status == "success":
            if is_browser_action:
                updates["snapshot_before_last_browser_action"] = str(
                    state.get("snapshot", "") or ""
                )
                updates["last_browser_action"] = _action_identity(request, tool_name)
            updates["snapshot"] = ""
        if needs_fresh_after_error:
            updates["snapshot"] = ""
            updates["needs_fresh_snapshot"] = True

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
        updates.update(
            _plan_completion_update(
                plan,
                current_step,
                compact,
                tool_name=tool_name,
            )
        )
        updates["error"] = ""
        updates["consecutive_failures"] = 0
        if tool_name != "browser_snapshot":
            updates["stale_snapshot_retries"] = 0
            updates["invalid_ref_recovery_count"] = 0
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
