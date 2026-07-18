"""Top-level agent graph nodes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.types import interrupt

from src.harness.memory import (
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)
from src.agent.subgraphs.observer.nodes import (
    create_observe_node,
    observe_node,
)
from src.agent.subgraphs.observer.utils import extract_element_refs
from src.agent.prompts import AGENT_USER_PROMPT
from src.agent.state import (
    AgentState,
)
from src.harness.tools import ToolRegistry
from .utils import (
    _replan_response,
    _message_content,
    _json_object,
    _format_plan,
    _normalize_tool_request,
    _terminal_guard,
    _tool_request_update,
    _fresh_snapshot_request,
    _stale_snapshot_retry_update,
    _bind_tools,
    _tool_call_to_request
)


def create_agent_node(
    llm: Any,
    tools: Sequence[Any] | None = None,
    tool_registry: ToolRegistry | None = None,
    history_builder: Callable[[AgentState], list[BaseMessage]] = ensure_message_history,
) -> Callable[[AgentState], Any]:
    """Create the reasoning node bound to an LLM."""

    registry = tool_registry or ToolRegistry(tools=tools)
    tool_bound_llm = None

    async def agent_node(state: AgentState) -> dict[str, Any]:
        nonlocal tool_bound_llm

        if tool_bound_llm is None:
            tool_bound_llm = _bind_tools(llm, await registry.get_all())

        terminal = _terminal_guard(state)
        if terminal is not None:
            return terminal

        if not state.get("plan"):
            return _replan_response("No plan is available.")

        messages = history_builder(state)
        stale_snapshot_update = _stale_snapshot_retry_update(state)
        if stale_snapshot_update.get("decision") == "replan":
            return stale_snapshot_update
        if state.get("needs_fresh_snapshot"):
            snapshot_request = _fresh_snapshot_request(state, messages)
            snapshot_request.update(stale_snapshot_update)
            return snapshot_request

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
                        refs=", ".join(
                            extract_element_refs(str(state.get("snapshot", "") or ""))
                        )
                        or "none",
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
                    **_replan_response("The model selected a tool call without a tool name."),
                }
            return _tool_request_update(state, messages, tool_request)

        if decision == "replan":
            return _replan_response(str(data.get("reason", "Replanning requested.")))

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
            "policy_event": {
                "decision": "approved",
                "reason": "Human approval was granted.",
                "tool_request": request,
                "human_response": approval,
            },
            "error": "",
        }

    reason = "Human approval was denied."
    return {
        "policy_decision": "blocked",
        "policy_event": {
            "decision": "blocked",
            "reason": reason,
            "tool_request": request,
            "human_response": approval,
        },
        "observation": reason,
        "error": reason,
        "messages": append_tool_message(
            list(state.get("messages") or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        ),
    }
