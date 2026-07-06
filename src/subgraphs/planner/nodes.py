"""Planner graph nodes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage

from src.agent.history import ensure_message_history
from src.agent.state import AgentState, PlanStep
from src.subgraphs.planner.prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_PROMPT


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


def _normalize_steps(raw_steps: Any, task: str) -> list[PlanStep]:
    if not isinstance(raw_steps, list):
        return [{"id": 1, "description": task, "status": "pending"}]

    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if isinstance(raw_step, str):
            description = raw_step.strip()
        elif isinstance(raw_step, dict):
            description = str(raw_step.get("description", "")).strip()
        else:
            description = ""

        if description:
            steps.append({"id": index, "description": description, "status": "pending"})

    return steps or [{"id": 1, "description": task, "status": "pending"}]


def create_plan_node(llm: Any) -> Callable[[AgentState], Any]:
    """Create an async planner node bound to an LLM."""

    async def plan_node(state: AgentState) -> dict[str, Any]:
        task = state.get("task", "").strip()
        observation = state.get("observation", "")
        messages = ensure_message_history(state)
        prior_replans = int(state.get("replan_count", 0) or 0)
        replan_count = prior_replans + 1 if state.get("plan") else prior_replans
        response = await llm.ainvoke(
            [
                *messages,
                HumanMessage(
                    content="\n\n".join(
                        [
                            PLANNER_SYSTEM_PROMPT,
                            PLANNER_USER_PROMPT.format(
                                task=task,
                                observation=observation or "No observation yet.",
                            ),
                        ]
                    )
                ),
            ]
        )

        data = _json_object(_message_content(response))
        plan = _normalize_steps(data.get("steps"), task or "Complete the task.")
        return {
            "plan": plan,
            "current_step": 0,
            "decision": "replan",
            "replan_count": replan_count,
            "error": "",
            "messages": messages,
        }

    return plan_node
