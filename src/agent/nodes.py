"""Top-level agent graph nodes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from src.agent.history import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)
from src.agent.observer_llm import compress_tool_result
from src.agent.observe import compile_observation
from src.agent.prompts import AGENT_USER_PROMPT
from src.agent.state import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_SNAPSHOT_RECOVERIES,
    AgentState,
    ToolRequest,
)


SNAPSHOT_RECOVERY_DEPTH = 4


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
        f"{step.get('id', index)}. {step.get('description', '')}"
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


def _terminal_guard(state: AgentState) -> dict[str, Any] | None:
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

    return None


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _snapshot_recovery_request(state: AgentState, request: ToolRequest) -> ToolRequest | None:
    if request.get("name") != "browser_snapshot":
        return None
    if int(state.get("snapshot_recovery_count", 0) or 0) >= MAX_SNAPSHOT_RECOVERIES:
        return None

    args = dict(request.get("args") or {})
    current_depth = _int_value(args.get("depth"), 0)
    args["depth"] = max(SNAPSHOT_RECOVERY_DEPTH, current_depth + 2)
    return with_tool_call_id(
        {
            "name": "browser_snapshot",
            "args": args,
            "reason": (
                "Recovery after repeated identical snapshots: capture a deeper "
                "Playwright MCP snapshot before replanning."
            ),
        }
    )


def _guard_tool_request(state: AgentState, request: ToolRequest) -> dict[str, Any] | None:
    if (
        state.get("last_tool") == request.get("name")
        and state.get("last_args", {}) == request.get("args", {})
        and int(state.get("repeat_count", 0) or 0) >= 2
    ):
        recovery_request = _snapshot_recovery_request(state, request)
        if recovery_request is not None:
            return {
                "decision": "tool_call",
                "tool_request": recovery_request,
                "policy_decision": "",
                "error": "",
                "snapshot_recovery_count": (
                    int(state.get("snapshot_recovery_count", 0) or 0) + 1
                ),
                **_request_tracking_update(state, recovery_request),
            }

        return {
            "decision": "replan",
            "observation": (
                "The same tool with the same arguments was requested three "
                "consecutive times. Replan before trying another action."
            ),
            "last_tool": request.get("name", ""),
            "last_args": request.get("args", {}),
            "repeat_count": 3,
        }

    return None


def _request_tracking_update(state: AgentState, request: ToolRequest) -> dict[str, Any]:
    tool_name = request.get("name", "")
    args = request.get("args", {})
    if state.get("last_tool") == tool_name and state.get("last_args", {}) == args:
        repeat_count = int(state.get("repeat_count", 0) or 0) + 1
    else:
        repeat_count = 1
    return {
        "last_tool": tool_name,
        "last_args": args,
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


def create_agent_node(
    llm: Any,
    tools: Sequence[Any] | None = None,
) -> Callable[[AgentState], Any]:
    """Create the reasoning node bound to an LLM."""

    tool_bound_llm = _bind_tools(llm, tools)

    async def agent_node(state: AgentState) -> dict[str, Any]:
        terminal = _terminal_guard(state)
        if terminal is not None:
            return terminal

        if not state.get("plan"):
            return {"decision": "replan", "observation": "No plan is available."}

        messages = ensure_message_history(state)
        response = await tool_bound_llm.ainvoke(
            [
                *messages,
                HumanMessage(
                    content=AGENT_USER_PROMPT.format(
                        task=state.get("task", ""),
                        plan=_format_plan(state),
                        current_step=state.get("current_step", 0),
                        observation=state.get("observation", "No observation yet."),
                        snapshot=state.get("snapshot", ""),
                        refs=", ".join(state.get("refs", [])) or "none",
                    )
                ),
            ]
        )

        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            tool_request = with_tool_call_id(_tool_call_to_request(tool_calls[0]))
            if tool_request.get("name"):
                return _tool_request_update(state, messages, tool_request)

        content = _message_content(response)
        data = _json_object(content)
        decision = str(data.get("decision", "")).strip()

        if decision == "tool_call":
            tool_request = with_tool_call_id(
                _normalize_tool_request(data.get("tool_request"))
            )
            if not tool_request.get("name"):
                return {
                    "decision": "replan",
                    "observation": "The model selected a tool call without a tool name.",
                }
            return _tool_request_update(state, messages, tool_request)

        if decision == "replan":
            return {
                "decision": "replan",
                "observation": str(data.get("reason", "Replanning requested.")),
            }

        if decision == "done":
            final_answer = str(data.get("final_answer", "") or content)
            return {
                "decision": "done",
                "final_answer": final_answer,
                "messages": append_final_ai_response(messages, final_answer),
            }

        return {
            "decision": "done",
            "final_answer": content,
            "messages": append_final_ai_response(messages, content),
        }

    return agent_node


def observe_node(state: AgentState) -> dict[str, Any]:
    """Compile executor output into MCP-aware observation state without an LLM."""

    return compile_observation(state)


def create_observe_node(observer_llm: Any | None = None) -> Callable[[AgentState], Any]:
    """Create an observer node whose LLM only sees the latest ToolResult."""

    async def _observe_node(state: AgentState) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        compact_observation = await compress_tool_result(result, observer_llm)
        return compile_observation(state, compact_observation)

    return _observe_node


def human_input_node(state: AgentState) -> dict[str, Any]:
    """Ask a human to approve a risky tool call through LangGraph interrupt."""

    request = state.get("tool_request") or {}
    approval = interrupt(
        {
            "kind": "tool_approval",
            "tool_request": request,
            "message": f"Approve tool execution: {request.get('name', '')}",
        }
    )

    approved = approval is True
    if isinstance(approval, str):
        approved = approval.strip().lower() in {"approve", "approved", "yes", "y", "true"}
    if isinstance(approval, dict):
        approved = bool(approval.get("approved"))

    if approved:
        return {
            "policy_decision": "approved",
            "human_approval": approval,
            "error": "",
        }

    reason = "Human approval was denied."
    return {
        "policy_decision": "blocked",
        "human_approval": approval,
        "observation": reason,
        "error": reason,
        "messages": append_tool_message(
            list(state.get("messages") or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        ),
    }
