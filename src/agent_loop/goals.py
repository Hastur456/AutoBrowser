"""Goal lifecycle boundary for one AutoBrowser task."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from src.agent_loop.events import EventEmitter


TaskRunner = Callable[[Any, str, Any, dict[str, Any]], Awaitable[Any]]
LatestStateLoader = Callable[
    [Mapping[str, Any], Any | None],
    Awaitable[dict[str, object] | None],
]
GoalStatus = Literal["completed", "failed", "cancelled", "blocked"]
DEFAULT_TASK_TIMEOUT_SECONDS = 300.0
DEFAULT_PROGRESS_TIMEOUT_SECONDS = 120.0
DEFAULT_LATEST_STATE_TIMEOUT_SECONDS = 15.0


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
    """Run one goal with lifecycle events and bounded failure paths."""

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
        timeout_seconds = _goal_timeout_seconds(self._session_config)
        progress_timeout_seconds = _goal_progress_timeout_seconds(
            self._session_config,
            timeout_seconds,
        )
        latest_state_timeout_seconds = _latest_state_timeout_seconds(
            self._session_config,
            timeout_seconds,
        )
        self._event_emitter.emit(
            "goal.started",
            source="harness.session",
            payload={"task": request.task},
            task_id=request.task_id,
            goal_id=request.goal_id,
        )
        try:
            result = await self._run_task_with_watchdog(
                request,
                task_config,
                timeout_seconds=timeout_seconds,
                progress_timeout_seconds=progress_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._latest_state_after_failure(
                task_config,
                timeout_seconds=latest_state_timeout_seconds,
            )
            self._event_emitter.emit(
                "goal.failed",
                source="harness.session",
                payload={"task": request.task, "error": exc},
                task_id=request.task_id,
                goal_id=request.goal_id,
            )
            raise
        except Exception as exc:
            await self._latest_state_after_failure(
                task_config,
                timeout_seconds=latest_state_timeout_seconds,
            )
            self._event_emitter.emit(
                "goal.failed",
                source="harness.session",
                payload={"task": request.task, "error": exc},
                task_id=request.task_id,
                goal_id=request.goal_id,
            )
            raise

        latest_state = await self._load_latest_state(
            task_config,
            result,
            timeout_seconds=latest_state_timeout_seconds,
            suppress_errors=True,
        )
        terminal_state = _find_terminal_state(latest_state) or _find_terminal_state(
            result
        )
        status = _goal_status_from_terminal_state(terminal_state)
        if status is None:
            nonterminal_error = RuntimeError(
                "Goal runner finished without a terminal agent state."
            )
            self._event_emitter.emit(
                "goal.failed",
                source="harness.session",
                payload={"task": request.task, "error": nonterminal_error},
                task_id=request.task_id,
                goal_id=request.goal_id,
            )
            raise nonterminal_error

        event_type = "goal.blocked" if status == "blocked" else "goal.completed"
        self._event_emitter.emit(
            event_type,
            source="harness.session",
            payload={"task": request.task, "result": result},
            task_id=request.task_id,
            goal_id=request.goal_id,
        )
        return GoalRunResult(
            task=request.task,
            task_id=request.task_id,
            goal_id=request.goal_id,
            result=result,
            latest_state=latest_state,
            status=status,
        )

    async def _run_task_with_watchdog(
        self,
        request: GoalRunRequest,
        task_config: dict[str, Any],
        *,
        timeout_seconds: float,
        progress_timeout_seconds: float,
    ) -> Any:
        if progress_timeout_seconds >= timeout_seconds:
            try:
                return await asyncio.wait_for(
                    self._task_runner(
                        self._harness,
                        request.task,
                        self._session_config,
                        task_config,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Goal runner timed out after {timeout_seconds:g} seconds."
                ) from exc

        task = asyncio.create_task(
            self._task_runner(
                self._harness,
                request.task,
                self._session_config,
                task_config,
            )
        )
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
        last_sequence = self._event_emitter.sequence
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        poll_interval = _watchdog_poll_interval(timeout_seconds)

        while True:
            await asyncio.sleep(min(poll_interval, max(deadline - loop.time(), 0.0)))
            current_sequence = self._event_emitter.sequence
            if current_sequence != last_sequence:
                last_sequence = current_sequence
                deadline = loop.time() + timeout_seconds
                continue
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Goal runner made no progress for {timeout_seconds:g} seconds."
                )

    async def _latest_state_after_failure(
        self,
        task_config: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, object] | None:
        return await self._load_latest_state(
            task_config,
            None,
            timeout_seconds=timeout_seconds,
            suppress_errors=True,
        )

    async def _load_latest_state(
        self,
        task_config: Mapping[str, Any],
        fallback: Any | None,
        *,
        timeout_seconds: float,
        suppress_errors: bool,
    ) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(
                self._latest_state_loader(task_config, fallback),
                timeout=timeout_seconds,
            )
        except Exception:
            if suppress_errors:
                terminal_state = _find_terminal_state(fallback)
                return terminal_state if terminal_state is not None else None
            raise


def _goal_timeout_seconds(session_config: Any) -> float:
    for attr in (
        "goal_timeout_seconds",
        "task_timeout_seconds",
        "goal_timeout",
        "task_timeout",
    ):
        value = getattr(session_config, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return DEFAULT_TASK_TIMEOUT_SECONDS


def _goal_progress_timeout_seconds(
    session_config: Any,
    total_timeout_seconds: float,
) -> float:
    for attr in (
        "goal_progress_timeout_seconds",
        "task_progress_timeout_seconds",
        "goal_watchdog_seconds",
        "task_watchdog_seconds",
        "watchdog_seconds",
    ):
        value = getattr(session_config, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return min(float(value), total_timeout_seconds)
    return min(DEFAULT_PROGRESS_TIMEOUT_SECONDS, total_timeout_seconds)


def _latest_state_timeout_seconds(
    session_config: Any,
    total_timeout_seconds: float,
) -> float:
    for attr in ("latest_state_timeout_seconds", "state_load_timeout_seconds"):
        value = getattr(session_config, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return min(float(value), total_timeout_seconds)
    return min(DEFAULT_LATEST_STATE_TIMEOUT_SECONDS, total_timeout_seconds)


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


def _find_terminal_state(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        final_answer = value.get("final_answer")
        if final_answer is not None:
            return dict(value)
        for nested_value in value.values():
            terminal_state = _find_terminal_state(nested_value)
            if terminal_state is not None:
                return terminal_state
    elif isinstance(value, list):
        for nested_value in value:
            terminal_state = _find_terminal_state(nested_value)
            if terminal_state is not None:
                return terminal_state
    return None


def _goal_status_from_terminal_state(state: Mapping[str, Any] | None) -> GoalStatus | None:
    if not state:
        return None

    final_answer = str(state.get("final_answer", "") or "").strip()
    if not final_answer:
        return None
    if final_answer.lower().startswith("blocked:"):
        return "blocked"
    decision = str(state.get("decision", "") or "").strip().lower()
    if decision in {"", "done"}:
        return "completed"
    if decision == "blocked":
        return "blocked"
    return "completed"


__all__ = [
    "GoalRunRequest",
    "GoalRunResult",
    "GoalRunner",
    "GoalStatus",
    "LatestStateLoader",
    "TaskRunner",
]
