from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.execution.loop import AgentLoopResult
from src.agent_loop.execution.state import LoopState
from src.agent_loop.goals import CANCELLED_REASON, GoalLimits, GoalRunner
from src.config import LoopSettings
from src.contracts import TaskRequest

LIMITS = GoalLimits(timeout_seconds=1.0, progress_timeout_seconds=1.0)


def native_result(
    final_answer: str = "",
    *,
    status: str = "done",
    **state_updates: Any,
) -> AgentLoopResult:
    """Build a terminal ``AgentLoopResult`` the way the native loop does."""

    state = LoopState(**state_updates)
    return AgentLoopResult(
        status=status,
        final_answer=final_answer,
        session_state=state.to_session_state(),
        state=state,
    )


def make_request() -> TaskRequest:
    return TaskRequest(
        task="inspect page",
        task_id="task-1",
        session_id="session-1",
        state_overrides={"task_id": "task-1"},
    )


def make_runner(
    task_runner: Any,
    sink: InMemoryEventSink,
    limits: GoalLimits = LIMITS,
) -> GoalRunner:
    return GoalRunner(
        task_runner=task_runner,
        events=EventEmitter(sink, session_id="session-1"),
        limits=limits,
    )


@pytest.mark.asyncio
async def test_goal_runner_emits_events_calls_task_runner_and_returns_result() -> None:
    sink = InMemoryEventSink()
    calls: list[TaskRequest] = []
    result = native_result(status="done", final_answer="Finished.", messages=["kept"])
    request = make_request()

    async def task_runner(call: TaskRequest) -> AgentLoopResult:
        calls.append(call)
        return result

    goal_result = await make_runner(task_runner, sink).run(request)

    assert goal_result.request is request
    assert goal_result.result is result
    assert goal_result.status == "completed"
    assert calls == [request]
    assert request.goal_id == request.task_id == "task-1"
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert [record.task_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[0].payload == {"task": "inspect page"}
    assert sink.records[1].payload == {
        "task": "inspect page",
        "status": "completed",
        "final_answer": "Finished.",
        "turns": 0,
    }


@pytest.mark.asyncio
async def test_goal_runner_terminal_payload_carries_no_session_state() -> None:
    sink = InMemoryEventSink()
    result = AgentLoopResult(
        status="done",
        final_answer="Finished.",
        session_state=LoopState(messages=["secret history"]).to_session_state(),
        state=LoopState(),
        turns=3,
    )

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        return result

    await make_runner(task_runner, sink).run(make_request())

    payload = sink.records[-1].to_json_dict()["payload"]
    assert payload == {
        "task": "inspect page",
        "status": "completed",
        "final_answer": "Finished.",
        "turns": 3,
    }
    assert "secret history" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, goal_status, event",
    [("blocked", "blocked", "goal.blocked"), ("cancelled", "cancelled", "goal.cancelled")],
)
async def test_goal_runner_emits_the_terminal_event_of_the_status(
    status: str,
    goal_status: str,
    event: str,
) -> None:
    sink = InMemoryEventSink()
    result = native_result(status=status, final_answer="Stopped.")

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        return result

    goal_result = await make_runner(task_runner, sink).run(make_request())

    assert goal_result.status == goal_status
    assert goal_result.result is result
    assert [record.type for record in sink.records] == ["goal.started", event]
    assert sink.records[1].payload == {
        "task": "inspect page",
        "status": goal_status,
        "final_answer": "Stopped.",
        "turns": 0,
    }


@pytest.mark.asyncio
async def test_goal_runner_emits_failure_event_and_reraises_original_exception() -> None:
    sink = InMemoryEventSink()
    error = RuntimeError("task failed")

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await make_runner(task_runner, sink).run(make_request())

    assert exc_info.value is error
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[1].payload == {"task": "inspect page", "status": "failed", "error": error}


@pytest.mark.asyncio
@pytest.mark.parametrize("progress_timeout", [1.0, 0.5])
async def test_goal_runner_emits_cancelled_event_and_reraises_cancellation(
    progress_timeout: float,
) -> None:
    sink = InMemoryEventSink()
    started = asyncio.Event()

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        started.set()
        await asyncio.Event().wait()
        return native_result(status="done", final_answer="never reached")

    limits = GoalLimits(timeout_seconds=1.0, progress_timeout_seconds=progress_timeout)
    goal = asyncio.create_task(make_runner(task_runner, sink, limits).run(make_request()))
    await started.wait()
    goal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await goal

    assert [record.type for record in sink.records] == ["goal.started", "goal.cancelled"]
    assert sink.records[1].payload == {
        "task": "inspect page",
        "status": "cancelled",
        "reason": CANCELLED_REASON,
    }


@pytest.mark.asyncio
async def test_goal_runner_times_out_and_emits_failure_event() -> None:
    sink = InMemoryEventSink()

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        await asyncio.sleep(0.1)
        return native_result(status="done", final_answer="never reached")

    limits = GoalLimits(timeout_seconds=0.01, progress_timeout_seconds=0.01)
    with pytest.raises(TimeoutError, match="timed out after 0.01"):
        await make_runner(task_runner, sink, limits).run(make_request())

    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert sink.records[1].payload["task"] == "inspect page"
    assert isinstance(sink.records[1].payload["error"], TimeoutError)


@pytest.mark.asyncio
async def test_goal_runner_watchdog_fails_when_events_stop() -> None:
    sink = InMemoryEventSink()
    cancelled = False

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return native_result(status="done", final_answer="never reached")

    limits = GoalLimits(timeout_seconds=1.0, progress_timeout_seconds=0.01)
    with pytest.raises(TimeoutError, match="made no progress for 0.01"):
        await make_runner(task_runner, sink, limits).run(make_request())

    assert cancelled is True
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert isinstance(sink.records[1].payload["error"], TimeoutError)


@pytest.mark.asyncio
async def test_goal_runner_rejects_non_agent_loop_result() -> None:
    sink = InMemoryEventSink()

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        return {"final_answer": "done"}  # type: ignore[return-value]

    with pytest.raises(RuntimeError, match="without a terminal goal state"):
        await make_runner(task_runner, sink).run(make_request())

    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]


@pytest.mark.asyncio
async def test_goal_runner_rejects_non_terminal_status() -> None:
    sink = InMemoryEventSink()

    async def task_runner(_request: TaskRequest) -> AgentLoopResult:
        return native_result(status="continue", final_answer="")

    with pytest.raises(RuntimeError, match="without a terminal goal state"):
        await make_runner(task_runner, sink).run(make_request())

    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]


def test_goal_limits_come_from_the_loop_settings() -> None:
    limits = GoalLimits.from_settings(
        LoopSettings(task_timeout_seconds=30.0, progress_timeout_seconds=10.0)
    )

    assert limits == GoalLimits(timeout_seconds=30.0, progress_timeout_seconds=10.0)
