"""Goal lifecycle boundary for one AutoBrowser task."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from src.agent_loop.events import EventEmitter


TaskRunner = Callable[[Any, str, Any, dict[str, Any]], Awaitable[Any]]
LatestStateLoader = Callable[
    [Mapping[str, Any], Any | None],
    Awaitable[dict[str, object] | None],
]
GoalStatus = Literal["completed", "failed", "cancelled", "blocked"]


@dataclass(frozen=True)
class GoalRunRequest:
    """Inputs needed to execute one task through the current engine path."""

    task: str
    task_id: str
    goal_id: str
    thread_id: str
    config: Mapping[str, Any]
    state_overrides: Mapping[str, object]


@dataclass(frozen=True)
class GoalRunResult:
    """Terminal data captured for one goal run."""

    task: str
    task_id: str
    goal_id: str
    result: Any
    latest_state: Mapping[str, object] | None
    status: GoalStatus


class GoalRunner:
    """Compatibility shell around the current task runner implementation."""

    def __init__(
        self,
        *,
        harness: Any,
        session_config: Any,
        task_runner: TaskRunner,
        event_emitter: EventEmitter,
        latest_state_loader: LatestStateLoader,
    ) -> None:
        self._harness = harness
        self._session_config = session_config
        self._task_runner = task_runner
        self._event_emitter = event_emitter
        self._latest_state_loader = latest_state_loader

    async def run(self, request: GoalRunRequest) -> GoalRunResult:
        """Run one goal and emit terminal lifecycle events."""

        task_config = dict(request.config)
        self._event_emitter.emit(
            "goal.started",
            source="harness.session",
            payload={"task": request.task},
            task_id=request.task_id,
            goal_id=request.goal_id,
        )
        try:
            result = await self._task_runner(
                self._harness,
                request.task,
                self._session_config,
                task_config,
            )
        except Exception as exc:
            latest_state = await self._latest_state_loader(task_config, None)
            self._terminal_result(
                request=request,
                result=exc,
                latest_state=latest_state,
                status="failed",
            )
            self._event_emitter.emit(
                "goal.failed",
                source="harness.session",
                payload={"task": request.task, "error": exc},
                task_id=request.task_id,
                goal_id=request.goal_id,
            )
            raise

        latest_state = await self._latest_state_loader(task_config, result)
        self._event_emitter.emit(
            "goal.completed",
            source="harness.session",
            payload={"task": request.task, "result": result},
            task_id=request.task_id,
            goal_id=request.goal_id,
        )
        return self._terminal_result(
            request=request,
            result=result,
            latest_state=latest_state,
            status="completed",
        )

    def _terminal_result(
        self,
        *,
        request: GoalRunRequest,
        result: Any,
        latest_state: Mapping[str, object] | None,
        status: GoalStatus,
    ) -> GoalRunResult:
        return GoalRunResult(
            task=request.task,
            task_id=request.task_id,
            goal_id=request.goal_id,
            result=result,
            latest_state=latest_state,
            status=status,
        )


__all__ = [
    "GoalRunRequest",
    "GoalRunResult",
    "GoalRunner",
    "GoalStatus",
    "LatestStateLoader",
    "TaskRunner",
]
