"""Context construction for the AutoBrowser harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agent.prompts import AGENT_SYSTEM_PROMPT
from src.agent.state import AgentState


class ContextBuilder:
    """Build prompt context and initial graph state for the agent loop."""

    def __init__(self, system_prompt: str | None = None) -> None:
        self._system_prompt = system_prompt or AGENT_SYSTEM_PROMPT

    def get_system_prompt(self) -> str:
        """Return the system prompt injected into durable message history."""

        return self._system_prompt

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


__all__ = ["ContextBuilder"]
