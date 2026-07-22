from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from src.harness.memory import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)
from src.agent.subgraphs.executor.nodes import _element_description_from_snapshot
from src.agent.subgraphs.observer.utils import has_invalid_ref_text
from src.agent.state import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_STEPS_WITHOUT_PLAN_ADVANCE,
    AgentState,
    ToolRequest,
)

SNAPSHOT_REUSE_MARKER = "browser_snapshot is already current"
REPEATED_SNAPSHOT_FINAL_ANSWER = (
    "Stopped because browser_snapshot was requested three consecutive times "
    "without a meaningful state change. Latest observation:\n\n{observation}"
)


def _message_content(response: Any) -> str:
    return str(getattr(response, "content", response))


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _format_plan(state: AgentState) -> str:
    plan = state.get("plan") or []
    if not plan:
        return "No plan yet."
    return "\n".join(
        (
            f"{step.get('id', index)}. "
            f"[{step.get('status', 'pending')}] "
            f"{step.get('description', '')}"
        )
        for index, step in enumerate(plan, start=1)
    )


def _normalize_tool_request(raw_request: Any) -> ToolRequest:
    if not isinstance(raw_request, dict):
        return {"name": "", "args": {}, "reason": ""}
    args = raw_request.get("args")
    return {
        "name": str(raw_request.get("name", "")).strip(),
        "args": args if isinstance(args, dict) else {},
        "reason": str(raw_request.get("reason", "")).strip(),
    }


def _blocked_response(state: AgentState, reason: str) -> dict[str, Any]:
    messages = ensure_message_history(state)
    return {
        "decision": "done",
        "final_answer": reason,
        "observation": reason,
        "error": reason,
        "messages": append_final_ai_response(messages, reason),
    }


def _done_response(
    state: AgentState,
    final_answer: str,
    *,
    messages: list[Any] | None = None,
    **updates: Any,
) -> dict[str, Any]:
    history = messages if messages is not None else ensure_message_history(state)
    response = {
        "decision": "done",
        "final_answer": final_answer,
        "messages": append_final_ai_response(history, final_answer),
        **_complete_plan_update(state),
    }
    response.update(updates)
    return response


def _complete_plan_update(state: AgentState) -> dict[str, Any]:
    plan = state.get("plan") or []
    if not plan:
        return {}

    return {
        "plan": [{**step, "status": "completed"} for step in plan],
        "current_step": len(plan),
    }


def _replan_response(observation: str, **updates: Any) -> dict[str, Any]:
    response = {
        "decision": "replan",
        "observation": observation,
        "stale_snapshot_retries": 0,
        "needs_fresh_snapshot": False,
    }
    response.update(updates)
    return response


def _terminal_guard(state: AgentState) -> dict[str, Any] | None:
    if state.get("decision") == "done" and state.get("final_answer"):
        return _done_response(state, str(state.get("final_answer", "") or ""))

    replan_count = int(state.get("replan_count", 0) or 0)
    if replan_count >= MAX_REPLANS:
        observation = str(state.get("observation", "") or "No observation available.")
        return _blocked_response(
            state,
            (
                f"Blocked: replanning reached the limit of {MAX_REPLANS}. "
                f"Last observation: {observation}"
            ),
        )

    failures = int(state.get("consecutive_failures", 0) or 0)
    if failures >= MAX_CONSECUTIVE_FAILURES:
        error = str(state.get("error", "") or "Unknown tool failure.")
        return _blocked_response(
            state,
            (
                "Blocked: tool execution failed "
                f"{MAX_CONSECUTIVE_FAILURES} consecutive times. Last error: {error}"
            ),
        )

    steps_without_plan_advance = int(state.get("steps_without_plan_advance", 0) or 0)
    if steps_without_plan_advance >= MAX_STEPS_WITHOUT_PLAN_ADVANCE:
        observation = str(state.get("observation", "") or "No observation available.")
        return _blocked_response(
            state,
            (
                "Blocked: the current plan step did not advance after "
                f"{MAX_STEPS_WITHOUT_PLAN_ADVANCE} successful tool steps. "
                f"Last observation: {observation}"
            ),
        )

    return None


def _has_reusable_current_snapshot(state: AgentState) -> bool:
    return (
        bool(str(state.get("snapshot", "") or "").strip())
        and not bool(state.get("needs_fresh_snapshot"))
        and not has_invalid_ref_text(state.get("error", ""))
    )


def _snapshot_reuse_was_blocked(state: AgentState) -> bool:
    policy_event = state.get("policy_event") or {}
    reason = str(policy_event.get("reason", "") or "")
    observation = str(state.get("observation", "") or "")
    error = str(state.get("error", "") or "")
    payload = "\n".join([reason, observation, error]).lower()
    return SNAPSHOT_REUSE_MARKER in payload


def _snapshot_reuse_replan_update(state: AgentState) -> dict[str, Any]:
    return _replan_response(
        (
            "browser_snapshot was just blocked because the current snapshot is "
            "already reusable. Continue from the existing snapshot and refs; use "
            "browser_find or browser_evaluate only if the current snapshot cannot "
            "answer the next step. Do not request another browser_snapshot just "
            "to vary depth."
        ),
        last_tool=state.get("last_tool", ""),
        last_args=state.get("last_args", {}),
        repeat_count=int(state.get("repeat_count", 0) or 0),
    )


def _browser_action_key(
    action: ToolRequest | dict[str, Any],
    snapshot: str = "",
) -> tuple[str, tuple[tuple[str, Any], ...]]:
    name = str(action.get("name", "") or "").lower()
    args = dict(action.get("args") or {})
    target = args.pop("ref", None) or args.pop("target", None)
    if target is not None:
        target_description = str(action.get("target_description", "") or "")
        if not target_description and snapshot:
            target_description = _element_description_from_snapshot(snapshot, str(target))
        args["target"] = target_description or str(target)
    return name, tuple(sorted(args.items()))


def _ineffective_action_repeat_update(
    state: AgentState,
    request: ToolRequest,
) -> dict[str, Any] | None:
    ineffective_actions = list(state.get("ineffective_browser_actions") or [])
    ineffective_action = state.get("ineffective_browser_action") or {}
    if ineffective_action:
        ineffective_actions.append(ineffective_action)
    if not ineffective_actions:
        return None

    count = int(state.get("ineffective_action_count", 0) or 0)
    if count <= 0:
        return None

    request_key = _browser_action_key(request, str(state.get("snapshot", "") or ""))
    if not any(_browser_action_key(action) == request_key for action in ineffective_actions):
        return None

    return _replan_response(
        (
            f"{request.get('name', 'browser action')} with the same target did not "
            "change the visible browser_snapshot. Replan before trying another "
            "action: choose a different visible control/ref, request a deeper "
            "snapshot, use evaluate only if the snapshot cannot expose the "
            "control, or use a fallback route."
        ),
        ineffective_action_count=count,
    )


def _guard_tool_request(state: AgentState, request: ToolRequest) -> dict[str, Any] | None:
    ineffective_repeat = _ineffective_action_repeat_update(state, request)
    if ineffective_repeat is not None:
        return ineffective_repeat

    if (
        request.get("name") == "browser_snapshot"
        and _has_reusable_current_snapshot(state)
        and _snapshot_reuse_was_blocked(state)
    ):
        return _snapshot_reuse_replan_update(state)

    snapshot = str(state.get("snapshot", "") or "")
    if (
        _repeat_tracking_key(
            state.get("last_tool", ""),
            state.get("last_args", {}),
            snapshot,
        )
        == _repeat_tracking_key(request.get("name", ""), request.get("args", {}), snapshot)
        and int(state.get("repeat_count", 0) or 0) >= 2
    ):
        if request.get("name") == "browser_snapshot":
            if int(state.get("unchanged_snapshot_count", 0) or 0) < 2:
                return None
            observation = str(state.get("observation", "") or "No observation available.")
            return {
                **_done_response(
                    state,
                    REPEATED_SNAPSHOT_FINAL_ANSWER.format(observation=observation),
                ),
                "last_tool": request.get("name", ""),
                "last_args": request.get("args", {}),
                "repeat_count": 3,
            }

        return {
            **_replan_response(
                "The same tool with the same arguments was requested three "
                "consecutive times. Replan before trying another action."
            ),
            "last_tool": request.get("name", ""),
            "last_args": request.get("args", {}),
            "repeat_count": 3,
        }

    return None


def _repeat_tracking_key(
    tool_name: Any,
    args: dict[str, Any] | None,
    snapshot: str = "",
) -> tuple[str, tuple[tuple[str, Any], ...]]:
    name = str(tool_name or "")
    normalized_args = dict(args or {})
    if name == "browser_snapshot":
        normalized_args.pop("depth", None)
    target = normalized_args.pop("ref", None) or normalized_args.pop("target", None)
    if target is not None:
        target_description = (
            _element_description_from_snapshot(snapshot, str(target)) if snapshot else ""
        )
        normalized_args["target"] = target_description or str(target)
    return name, tuple(sorted(normalized_args.items()))


def _request_tracking_update(state: AgentState, request: ToolRequest) -> dict[str, Any]:
    tool_name = request.get("name", "")
    args = request.get("args", {})
    snapshot = str(state.get("snapshot", "") or "")
    if _repeat_tracking_key(state.get("last_tool", ""), state.get("last_args", {}), snapshot) == (
        _repeat_tracking_key(tool_name, args, snapshot)
    ):
        repeat_count = int(state.get("repeat_count", 0) or 0) + 1
    else:
        repeat_count = 1
    tool_request = {**request, "args": args}
    return {
        "last_tool": tool_name,
        "last_args": args,
        "last_tool_request": tool_request,
        "repeat_count": repeat_count,
    }


def _tool_request_update(
    state: AgentState,
    messages: list[Any],
    tool_request: ToolRequest,
) -> dict[str, Any]:
    guarded = _guard_tool_request(state, tool_request)
    if guarded is not None:
        guarded_request = guarded.get("tool_request")
        if guarded.get("decision") == "tool_call" and isinstance(guarded_request, dict):
            guarded["messages"] = append_ai_tool_call(messages, guarded_request)
            return guarded

        if guarded.get("decision") == "done":
            return guarded

        messages_with_call = append_ai_tool_call(messages, tool_request)
        guarded["messages"] = append_tool_message(
            messages_with_call,
            tool_request,
            (
                f"{tool_request.get('name', '')}\n\n"
                f"{guarded.get('observation', '')}"
            ),
        )
        return guarded

    return {
        "decision": "tool_call",
        "tool_request": tool_request,
        "policy_decision": "",
        "error": "",
        "messages": append_ai_tool_call(messages, tool_request),
        **_request_tracking_update(state, tool_request),
    }


def _fresh_snapshot_request(state: AgentState, messages: list[Any]) -> dict[str, Any]:
    recovery_count = int(state.get("invalid_ref_recovery_count", 0) or 0)
    request = with_tool_call_id(
        {
            "name": "browser_snapshot",
            "args": {},
            "reason": (
                "Previous browser ref no longer exists in Playwright MCP. "
                "Capture a fresh snapshot before any ref-based action."
            ),
        }
    )
    return {
        "decision": "tool_call",
        "tool_request": request,
        "policy_decision": "",
        "error": str(state.get("error", "") or ""),
        "needs_fresh_snapshot": False,
        "invalid_ref_recovery_count": recovery_count + 1,
        "messages": append_ai_tool_call(messages, request),
        "last_tool": request["name"],
        "last_args": request["args"],
        "last_tool_request": request,
        "repeat_count": 1,
    }


def _stale_snapshot_retry_update(state: AgentState) -> dict[str, Any]:
    if not state.get("needs_fresh_snapshot"):
        return {"stale_snapshot_retries": 0}

    if not has_invalid_ref_text(state.get("error", "")):
        return {"stale_snapshot_retries": 0}

    if int(state.get("invalid_ref_recovery_count", 0) or 0) <= 0:
        return {"stale_snapshot_retries": 0}

    retries = int(state.get("stale_snapshot_retries", 0) or 0) + 1
    if retries >= 2:
        return _replan_response(
            (
                "A fresh browser_snapshot did not resolve the invalid ref twice. "
                "Replan before attempting another ref-based browser action."
            ),
            stale_snapshot_retries=0,
            invalid_ref_recovery_count=0,
            snapshot="",
        )

    return {"stale_snapshot_retries": retries}


def _bind_tools(llm: Any, tools: Sequence[Any] | None) -> Any:
    if not tools or not hasattr(llm, "bind_tools"):
        return llm
    try:
        return llm.bind_tools(tools)
    except NotImplementedError:
        return llm


def _tool_call_to_request(tool_call: Any) -> ToolRequest:
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        return {
            "name": str(tool_call.get("name", "")).strip(),
            "args": args if isinstance(args, dict) else {},
            "id": str(tool_call.get("id", "")).strip(),
            "reason": "Selected by bound tool call.",
        }

    args = getattr(tool_call, "args", None)
    return {
        "name": str(getattr(tool_call, "name", "")).strip(),
        "args": args if isinstance(args, dict) else {},
        "id": str(getattr(tool_call, "id", "")).strip(),
        "reason": "Selected by bound tool call.",
    }
