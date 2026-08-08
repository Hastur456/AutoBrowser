"""Top-level agent graph nodes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.types import interrupt

from src.agent_loop.adapters.langgraph import proposed_action_to_legacy_update
from src.agent_loop.model import ModelDriver
from src.agent.subgraphs.observer.nodes import (
    create_observe_node,
    observe_node,
)
from src.agent.subgraphs.observer.utils import extract_element_refs
from src.agent.state import (
    AgentState,
)
from src.harness.context import ContextBuilder
from src.harness.tools import ToolRegistry
from src.harness.tools import tool_name as registered_tool_name
from .utils import (
    _replan_response,
    _message_content,
    _json_object,
    _normalize_tool_request,
    _terminal_guard,
    _tool_request_update,
    _done_response,
    _fresh_snapshot_request,
    _pending_tab_activation_request,
    _stale_snapshot_retry_update,
    _bind_tools,
    _tool_call_to_request,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)


def create_agent_node(
    llm: Any,
    tools: Sequence[Any] | None = None,
    tool_registry: ToolRegistry | None = None,
    history_builder: Callable[[AgentState], list[BaseMessage]] = ensure_message_history,
    context_builder: ContextBuilder | None = None,
) -> Callable[[AgentState], Any]:
    """Create the reasoning node bound to an LLM."""

    registry = tool_registry or ToolRegistry(tools=tools)
    prompt_context = context_builder or ContextBuilder()
    model_driver = ModelDriver(llm, tool_registry=registry)
    browser_tabs_available = False
    available_tools_cache: Sequence[Any] | None = None

    async def agent_node(state: AgentState) -> dict[str, Any]:
        nonlocal browser_tabs_available, available_tools_cache

        if available_tools_cache is None:
            available_tools_cache = await registry.get_all()
            browser_tabs_available = any(
                registered_tool_name(tool) == "browser_tabs"
                for tool in available_tools_cache
            )

        terminal = _terminal_guard(state)
        if terminal is not None:
            return terminal

        if not state.get("plan"):
            return _replan_response("No plan is available.")

        messages = history_builder(state)
        pending_tab_request = _pending_tab_activation_request(state)
        if pending_tab_request is not None and browser_tabs_available:
            return _tool_request_update(state, messages, pending_tab_request)

        stale_snapshot_update = _stale_snapshot_retry_update(state)
        if stale_snapshot_update.get("decision") == "replan":
            return stale_snapshot_update
        if state.get("needs_fresh_snapshot"):
            snapshot_request = _fresh_snapshot_request(state, messages)
            snapshot_request.update(stale_snapshot_update)
            return snapshot_request

        turn_prompt = prompt_context.build_turn_prompt(
            state,
            available_tools_cache if available_tools_cache is not None else tools,
        )
        model_turn = await model_driver.invoke(
            [
                *messages,
                HumanMessage(content=turn_prompt),
            ],
            tools=available_tools_cache if available_tools_cache is not None else tools,
        )
        if model_turn.actions:
            return proposed_action_to_legacy_update(
                model_turn.actions[0],
                state,
                messages=messages,
            )

        response = model_turn.response
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
            return _done_response(state, final_answer, messages=messages)

        return _done_response(state, content, messages=messages)

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
