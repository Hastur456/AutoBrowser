from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.goals import GoalRunRequest, GoalRunResult, GoalRunner, GoalStatus


def make_request() -> GoalRunRequest:
    return GoalRunRequest(
        task="inspect page",
        task_id="task-1",
        goal_id="task-1",
        thread_id="session-1",
        config={"configurable": {"thread_id": "session-1"}},
        state_overrides={"task_id": "task-1"},
    )


@pytest.mark.asyncio
async def test_goal_runner_emits_events_calls_task_runner_and_returns_result() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    calls: list[tuple[Any, str, Any, dict[str, Any]]] = []
    state_calls: list[tuple[dict[str, Any], Any | None]] = []
    harness = object()
    session_config = object()
    result = {"final_answer": "done"}
    latest_state = {"messages": ["checkpoint"], "final_answer": "done"}

    async def task_runner(
        call_harness: Any,
        task: str,
        call_config: Any,
        task_config: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append((call_harness, task, call_config, task_config))
        return result

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return latest_state

    runner = GoalRunner(
        harness=harness,
        session_config=session_config,
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
    )

    goal_result = await runner.run(make_request())

    assert goal_result.task == "inspect page"
    assert goal_result.task_id == "task-1"
    assert goal_result.goal_id == "task-1"
    assert goal_result.result == result
    assert goal_result.latest_state == latest_state
    assert goal_result.status == "completed"
    assert calls == [
        (harness, "inspect page", session_config, {"configurable": {"thread_id": "session-1"}})
    ]
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, result)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert [record.task_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[0].payload == {"task": "inspect page"}
    assert sink.records[1].payload == {"task": "inspect page", "result": result}


@pytest.mark.asyncio
async def test_goal_runner_emits_failure_event_and_reraises_original_exception() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []
    terminal_results: list[GoalRunResult] = []
    error = RuntimeError("task failed")
    latest_state = {"messages": ["checkpoint before failure"]}

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        raise error

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return latest_state

    class RecordingGoalRunner(GoalRunner):
        def _terminal_result(
            self,
            *,
            request: GoalRunRequest,
            result: Any,
            latest_state: Mapping[str, object] | None,
            status: GoalStatus,
        ) -> GoalRunResult:
            goal_result = super()._terminal_result(
                request=request,
                result=result,
                latest_state=latest_state,
                status=status,
            )
            terminal_results.append(goal_result)
            return goal_result

    runner = RecordingGoalRunner(
        harness=object(),
        session_config=object(),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await runner.run(make_request())

    assert exc_info.value is error
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, None)]
    assert len(terminal_results) == 1
    assert terminal_results[0].task == "inspect page"
    assert terminal_results[0].task_id == "task-1"
    assert terminal_results[0].goal_id == "task-1"
    assert terminal_results[0].result is error
    assert terminal_results[0].latest_state == latest_state
    assert terminal_results[0].status == "failed"
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert [record.task_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[1].payload == {"task": "inspect page", "error": error}
