"""Goal lifecycle boundary for one AutoBrowser task."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent_loop.events import EventEmitter
from src.config import LoopSettings, get_settings
from src.contracts import GoalStatus, TaskRequest, goal_status_from_completion

if TYPE_CHECKING:
    from src.agent_loop.execution.loop import AgentLoopResult


#: Runs one task to a terminal result; ``Agent.run`` is the production one.
TaskRunner = Callable[[TaskRequest], Awaitable["AgentLoopResult"]]


@dataclass(frozen=True)
class GoalLimits:
    """Time budgets of one goal run."""

    #: Wall-clock budget for the whole task.
    timeout_seconds: float
    #: Silence on the event stream that counts as a stall; never above the total budget.
    progress_timeout_seconds: float

    @classmethod
    def from_settings(cls, loop: LoopSettings | None = None) -> GoalLimits:
        loop = loop or get_settings().loop
        return cls(
            timeout_seconds=loop.task_timeout_seconds,
            progress_timeout_seconds=min(loop.progress_timeout_seconds, loop.task_timeout_seconds),
        )


@dataclass(frozen=True)
class GoalRunResult:
    """Terminal data captured for one goal run."""

    request: TaskRequest
    result: AgentLoopResult
    status: GoalStatus


class GoalRunner:
    """Run one goal with lifecycle events and bounded failure paths.

    Owns only the lifecycle around a task: ``goal.*`` events, the wall-clock timeout and the
    progress watchdog. What the task does is the ``task_runner``'s business.
    """

    def __init__(
        self,
        *,
        task_runner: TaskRunner,
        events: EventEmitter,
        limits: GoalLimits | None = None,
    ) -> None:
        self._task_runner = task_runner
        self._events = events
        self._limits = limits or GoalLimits.from_settings()

    async def run(self, request: TaskRequest) -> GoalRunResult:
        """Run one goal and emit terminal lifecycle events from the native result.

        The task runner returns a terminal :class:`AgentLoopResult` whose ``status`` is
        already explicit, so this method does not compile observations or infer completion
        from agent-internal state.
        """

        self._emit("goal.started", request, {"task": request.task})
        try:
            result = await self._run_with_watchdog(request)
        except Exception as exc:
            self._emit("goal.failed", request, {"task": request.task, "error": exc})
            raise

        status = _goal_status_from_result(result)
        if status is None:
            nonterminal_error = RuntimeError(
                "Goal runner finished without a terminal goal state."
            )
            self._emit("goal.failed", request, {"task": request.task, "error": nonterminal_error})
            raise nonterminal_error

        self._emit(_goal_event_type(status), request, {"task": request.task, "result": result})
        return GoalRunResult(request=request, result=result, status=status)

    def _emit(self, event_type: str, request: TaskRequest, payload: dict[str, Any]) -> None:
        self._events.emit(
            event_type,
            source="harness.session",
            payload=payload,
            task_id=request.task_id,
            goal_id=request.goal_id,
        )

    async def _run_with_watchdog(self, request: TaskRequest) -> AgentLoopResult:
        timeout_seconds = self._limits.timeout_seconds
        progress_timeout_seconds = min(self._limits.progress_timeout_seconds, timeout_seconds)
        if progress_timeout_seconds >= timeout_seconds:
            try:
                return await asyncio.wait_for(self._task_runner(request), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Goal runner timed out after {timeout_seconds:g} seconds."
                ) from exc

        task = asyncio.create_task(self._task_runner(request))
        watchdog = asyncio.create_task(self._watch_progress(progress_timeout_seconds))
        try:
            done, _pending = await asyncio.wait(
                {task, watchdog},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _cancel_pending(task)
                await _cancel_pending(watchdog)
                raise TimeoutError(
                    f"Goal runner timed out after {timeout_seconds:g} seconds."
                )
            if task in done:
                await _cancel_pending(watchdog)
                return await task

            await _cancel_pending(task)
            return await watchdog
        finally:
            await _cancel_pending(task)
            await _cancel_pending(watchdog)

    async def _watch_progress(self, timeout_seconds: float) -> None:
        last_sequence = self._events.sequence
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        poll_interval = _watchdog_poll_interval(timeout_seconds)

        while True:
            await asyncio.sleep(min(poll_interval, max(deadline - loop.time(), 0.0)))
            current_sequence = self._events.sequence
            if current_sequence != last_sequence:
                last_sequence = current_sequence
                deadline = loop.time() + timeout_seconds
                continue
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Goal runner made no progress for {timeout_seconds:g} seconds."
                )


def _goal_status_from_result(result: Any) -> GoalStatus | None:
    """Derive a terminal goal status from a native ``AgentLoopResult``.

    Imported lazily so importing this module does not pull in the engine and its resource
    composition. A result that is not an ``AgentLoopResult`` (or carries a non-terminal
    status such as ``"continue"``) yields ``None`` so ``run()`` fails it explicitly.
    """

    from src.agent_loop.execution.loop import AgentLoopResult

    if isinstance(result, AgentLoopResult):
        return goal_status_from_completion(result.status)
    return None


def _goal_event_type(status: GoalStatus) -> str:
    """Map a terminal goal status to its lifecycle event name."""

    if status == "blocked":
        return "goal.blocked"
    if status == "cancelled":
        return "goal.cancelled"
    return "goal.completed"


def _watchdog_poll_interval(timeout_seconds: float) -> float:
    return min(max(timeout_seconds / 4.0, 0.01), 5.0)


async def _cancel_pending(task: asyncio.Task[Any]) -> None:
    if task.done():
        with suppress(asyncio.CancelledError, Exception):
            task.exception()
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


__all__ = [
    "GoalLimits",
    "GoalRunResult",
    "GoalRunner",
    "GoalStatus",
    "TaskRunner",
]
