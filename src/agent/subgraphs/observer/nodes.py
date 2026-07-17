"""Observation translation for executor results."""

from __future__ import annotations

from typing import Any

from src.harness.memory import append_tool_message, tool_result_message_content
from src.agent.subgraphs.observer.observer_llm import fallback_compact_observation
from src.agent.state import AgentState, CompactToolObservation
from .utils import (
    extract_element_refs,
    _has_invalid_ref_error,
    _observation_lines,
    _plan_completion_update
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
    is_snapshot = tool_name == "browser_snapshot" and status == "success"
    has_invalid_ref_error = status == "error" and _has_invalid_ref_error(result)
    refs = extract_element_refs(content) if is_snapshot else []
    observation = "\n\n".join(
        _observation_lines(result, compact, refs, compress=compress_tool_output)
    )
    request = state.get("tool_request") or {}
    messages = list(state.get("messages") or [])
    tool_message = tool_result_message_content(
        result,
        compact,
        refs,
        observation,
        compress=compress_tool_output,
    )

    updates: dict[str, Any] = {
        "observation": observation,
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
        "messages": append_tool_message(messages, request, tool_message),
    }

    if is_snapshot:
        updates["snapshot"] = content
        updates["refs"] = refs
        updates["needs_fresh_snapshot"] = False
    elif is_browser_tool:
        updates["snapshot"] = ""
        updates["refs"] = []
        if has_invalid_ref_error:
            updates["needs_fresh_snapshot"] = True

    if status == "success":
        plan = state.get("plan") or []
        current_step = int(state.get("current_step", 0) or 0)
        updates.update(_plan_completion_update(plan, current_step, compact))
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
