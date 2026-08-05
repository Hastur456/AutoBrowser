"""Goal outcome compilation boundary for the transitional agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


GoalStatus = Literal["completed", "failed", "cancelled", "blocked"]
CompletionStatus = Literal["continue", "done", "blocked", "cancelled"]


@dataclass(frozen=True)
class GoalState:
    """Provider-neutral state used by GoalRunner to finish a goal."""

    status: CompletionStatus
    latest_state: Mapping[str, object] | None
    result: Any


class ObservationCompiler(Protocol):
    """Compile engine-specific output into provider-neutral goal state."""

    def compile(
        self,
        *,
        latest_state: Mapping[str, object] | None,
        result: Any,
    ) -> GoalState:
        """Return the goal state visible to the lifecycle runner."""


class CompletionGuard(Protocol):
    """Decide whether a compiled goal state is terminal."""

    def status(self, goal_state: GoalState) -> CompletionStatus:
        """Return the completion status for a compiled goal state."""


class GoalStateCompletionGuard:
    """Completion guard for already-compiled goal state."""

    def status(self, goal_state: GoalState) -> CompletionStatus:
        return goal_state.status


class LegacyAgentStateObservationCompiler:
    """Adapter from current LangGraph AgentState-shaped output to GoalState.

    This is intentionally the only transition point that knows about
    ``final_answer`` and ``decision``. Replace it with the real observer-backed
    compiler when the graph starts emitting provider-neutral goal state.
    """

    def compile(
        self,
        *,
        latest_state: Mapping[str, object] | None,
        result: Any,
    ) -> GoalState:
        source = _find_terminal_agent_state(latest_state)
        if source is None:
            source = _find_terminal_agent_state(result)
        status = _completion_status_from_agent_state(source)
        return GoalState(status=status, latest_state=latest_state, result=result)


def goal_status_from_completion(status: CompletionStatus) -> GoalStatus | None:
    """Map completion guard status into terminal goal status."""

    if status == "done":
        return "completed"
    if status in {"blocked", "cancelled"}:
        return status
    return None


def _find_terminal_agent_state(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        final_answer = value.get("final_answer")
        if final_answer is not None:
            return dict(value)
        for nested_value in value.values():
            terminal_state = _find_terminal_agent_state(nested_value)
            if terminal_state is not None:
                return terminal_state
    elif isinstance(value, list):
        for nested_value in value:
            terminal_state = _find_terminal_agent_state(nested_value)
            if terminal_state is not None:
                return terminal_state
    return None


def _completion_status_from_agent_state(
    state: Mapping[str, Any] | None,
) -> CompletionStatus:
    if not state:
        return "continue"

    final_answer = str(state.get("final_answer", "") or "").strip()
    if not final_answer:
        return "continue"
    if final_answer.lower().startswith("blocked:"):
        return "blocked"
    decision = str(state.get("decision", "") or "").strip().lower()
    if decision == "blocked":
        return "blocked"
    return "done"


__all__ = [
    "CompletionGuard",
    "CompletionStatus",
    "GoalState",
    "GoalStateCompletionGuard",
    "GoalStatus",
    "LegacyAgentStateObservationCompiler",
    "ObservationCompiler",
    "goal_status_from_completion",
]
