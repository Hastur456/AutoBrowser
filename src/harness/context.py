"""Context construction for the AutoBrowser harness."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from src.agent_loop.context import ContextAssembler
from src.agent_loop.context import AssembledContext
from src.agent_loop.prompts import (
    AGENT_SYSTEM_PROMPT,
    AGENT_USER_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_PROMPT,
)
from src.state import AgentState

CONTEXT_MODE_LEGACY = "legacy"
CONTEXT_MODE_ASSEMBLED = "assembled"


class ContextBuilder:
    """Build prompt context and initial graph state for the agent loop."""

    def __init__(
        self,
        system_prompt: str | None = None,
        *,
        context_mode: str | None = None,
        assembler: ContextAssembler | None = None,
    ) -> None:
        self._system_prompt = system_prompt or AGENT_SYSTEM_PROMPT
        self._context_mode = (context_mode or os.getenv("AUTOBROWSER_CONTEXT_MODE") or CONTEXT_MODE_LEGACY).strip().lower()
        self._assembler = assembler or ContextAssembler()

    def get_system_prompt(self) -> str:
        """Return the system prompt injected into durable message history."""

        return self._system_prompt

    def build_turn_prompt(
        self,
        state: Mapping[str, Any],
        tools: Sequence[Any] | None = None,
    ) -> str:
        """Return the per-turn prompt for the current agent state."""

        if self._context_mode == CONTEXT_MODE_ASSEMBLED:
            assembled = self._assembler.assemble(state, tools=tools)
            prompt = assembled.turn_prompt.strip()
            if prompt:
                return f"{prompt}\n\nChoose the next action."
            return "Choose the next action."

        return AGENT_USER_PROMPT.format(
            task=state.get("task", ""),
            plan=_format_plan(state.get("plan")),
            current_step=state.get("current_step", 0),
            observation=state.get("observation", "No observation yet."),
            consecutive_failures=state.get("consecutive_failures", 0),
            repeat_count=state.get("repeat_count", 0),
            snapshot=state.get("snapshot", ""),
            refs=_extract_refs(state.get("snapshot", "")),
        )

    def build_plan_prompt(self, state: Mapping[str, Any]) -> str:
        """Return the planner prompt for the current state.

        Mirrors ``plan_node`` (``src/agent/subgraphs/planner/nodes.py``): the planner
        message is the ``PLANNER_SYSTEM_PROMPT`` and ``PLANNER_USER_PROMPT`` joined by a
        blank line, with the observation defaulting to ``"No observation yet."``. This is
        the sanctioned prompt-assembly boundary for the engine-native loop, which must not
        import planner prompts from ``src/agent/`` directly.
        """

        task = str(state.get("task", "") or "").strip()
        observation = str(state.get("observation", "") or "")
        return "\n\n".join(
            [
                PLANNER_SYSTEM_PROMPT,
                PLANNER_USER_PROMPT.format(
                    task=task,
                    observation=observation or "No observation yet.",
                ),
            ]
        )

    def build_assembled_context(
        self,
        state: Mapping[str, Any],
        tools: Sequence[Any] | None = None,
    ) -> AssembledContext:
        """Expose the deterministic assembled context when requested."""

        return self._assembler.assemble(state, tools=tools)

    def build_initial_state(
        self,
        task: str,
        overrides: Mapping[str, Any] | None = None,
    ) -> AgentState:
        """Build the initial state passed into the compiled LangGraph."""

        state: AgentState = {"task": task}
        if overrides:
            state.update(overrides)
        return state


def _format_plan(plan: Any) -> str:
    if not plan:
        return "No plan yet."
    if isinstance(plan, str):
        return plan
    if not isinstance(plan, Sequence):
        return str(plan)
    return "\n".join(
        f"{step.get('id', index)}. [{step.get('status', 'pending')}] "
        f"{step.get('description', '')}"
        for index, step in enumerate(plan, start=1)
        if isinstance(step, Mapping)
    )


def _extract_refs(snapshot: Any) -> str:
    refs = re.findall(r"\bref=([A-Za-z0-9_-]+)", str(snapshot or ""))
    return ", ".join(dict.fromkeys(refs)) or "none"


__all__ = ["CONTEXT_MODE_ASSEMBLED", "CONTEXT_MODE_LEGACY", "ContextBuilder"]
