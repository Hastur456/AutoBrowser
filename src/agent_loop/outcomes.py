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


def goal_status_from_completion(status: CompletionStatus) -> GoalStatus | None:
    """Map completion guard status into terminal goal status."""

    if status == "done":
        return "completed"
    if status in {"blocked", "cancelled"}:
        return status
    return None


__all__ = [
    "CompletionGuard",
    "CompletionStatus",
    "GoalState",
    "GoalStateCompletionGuard",
    "GoalStatus",
    "ObservationCompiler",
    "goal_status_from_completion",
]
