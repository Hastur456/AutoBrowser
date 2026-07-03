"""Top-level agent graph nodes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from src.agent.prompts import AGENT_SYSTEM_PROMPT, AGENT_USER_PROMPT
from src.agent.state import AgentDecision, AgentState, ToolRequest


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


def create_agent_node(llm: Any) -> Callable[[AgentState], Any]:
    """Create the reasoning node bound to an LLM."""

    async def agent_node(state: AgentState) -> dict[str, Any]:
        if not state.get("plan"):
            return {"decision": "replan", "observation": "No plan is available."}

        response = await llm.ainvoke(
            [
                SystemMessage(content=AGENT_SYSTEM_PROMPT),
                HumanMessage(
                    content=AGENT_USER_PROMPT.format(
                        task=state.get("task", ""),
                        plan=_format_plan(state),
                        current_step=state.get("current_step", 0),
                        observation=state.get("observation", "No observation yet."),
                        history="\n".join(state.get("history", [])[-5:]) or "No history yet.",
                    )
                ),
            ]
        )

        content = _message_content(response)
        data = _json_object(content)
        decision = str(data.get("decision", "")).strip()

        if decision == "tool_call":
            tool_request = _normalize_tool_request(data.get("tool_request"))
            if not tool_request.get("name"):
                return {
                    "decision": "replan",
                    "observation": "The model selected a tool call without a tool name.",
                }
            return {
                "decision": "tool_call",
                "tool_request": tool_request,
                "policy_decision": "",
                "error": "",
            }

        if decision == "replan":
            return {
                "decision": "replan",
                "observation": str(data.get("reason", "Replanning requested.")),
            }

        if decision == "done":
            return {
                "decision": "done",
                "final_answer": str(data.get("final_answer", "") or content),
            }

        return {
            "decision": "done",
            "final_answer": content,
        }

    return agent_node


def observe_node(state: AgentState) -> dict[str, Any]:
    """Convert executor output into an observation for the next reasoning turn."""

    result = state.get("tool_result") or {}
    tool_name = result.get("name", "tool")
    status = result.get("status", "error")
    content = result.get("content") or result.get("error") or "No tool output."
    observation = f"{tool_name} returned {status}: {content}"

    history = list(state.get("history", []))
    history.append(observation)

    return {
        "observation": observation,
        "history": history,
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
    }


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
    }
