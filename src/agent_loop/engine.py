"""Explicit agent loop engine facade for the transitional runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.agent_loop.events import EventEmitter
from src.agent_loop.goals import GoalRunRequest, GoalRunResult, GoalRunner
from src.agent_loop.outcomes import CompletionGuard, ObservationCompiler


@dataclass(frozen=True)
class AgentLoopResult:
    """Terminal data returned by the explicit agent loop shell."""

    task: str
    task_id: str
    goal_id: str
    result: Any
    latest_state: Mapping[str, object] | None
    status: str


class AgentLoopEngine:
    """Compatibility shell that makes the explicit loop boundary visible."""

    def __init__(
        self,
        *,
        harness: Any,
        session_config: Any,
        task_runner: Callable[[Any, str, Any, dict[str, Any]], Awaitable[Any]],
        event_emitter: EventEmitter,
        latest_state_loader: Callable[
            [Mapping[str, Any], Any | None],
            Awaitable[dict[str, object] | None],
        ],
        observation_compiler: ObservationCompiler | None = None,
        completion_guard: CompletionGuard | None = None,
    ) -> None:
        self._runner = GoalRunner(
            harness=harness,
            session_config=session_config,
            task_runner=task_runner,
            event_emitter=event_emitter,
            latest_state_loader=latest_state_loader,
            observation_compiler=observation_compiler,
            completion_guard=completion_guard,
        )

    async def run(self, request: GoalRunRequest) -> AgentLoopResult:
        """Run one goal through the explicit engine shell."""

        result = await self._runner.run(request)
        return AgentLoopResult(
            task=result.task,
            task_id=result.task_id,
            goal_id=result.goal_id,
            result=result.result,
            latest_state=result.latest_state,
            status=result.status,
        )


__all__ = ["AgentLoopEngine", "AgentLoopResult"]
