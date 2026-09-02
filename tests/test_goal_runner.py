from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.goals import GoalRunRequest, GoalRunner
from src.agent_loop.outcomes import GoalState


class DictResultCompiler:
    """GoalState compiler for plain-dict task-runner results.

    The engine-native loop returns an ``AgentLoopResult``, so
    ``NativeObservationCompiler`` is the production default. These lifecycle tests
    inject raw dict task-runners instead, so they provide a small compiler that
    derives a terminal status from a ``final_answer`` the way the removed legacy
    compiler did.
    """

    def compile(
        self,
        *,
        latest_state: Mapping[str, object] | None,
        result: Any,
    ) -> GoalState:
        if isinstance(result, Mapping):
            nested = result.get("agent") if isinstance(result.get("agent"), Mapping) else result
            answer = str(nested.get("final_answer", "") or "")
            if answer:
                status = "blocked" if answer.startswith("Blocked:") else "done"
                return GoalState(status=status, latest_state=latest_state, result=result)
        return GoalState(status="continue", latest_state=latest_state, result=result)


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
        observation_compiler=DictResultCompiler(),
    )

    goal_result = await runner.run(make_request())

    assert goal_result.task == "inspect page"
    assert goal_result.task_id == "task-1"
    assert goal_result.goal_id == "task-1"
    assert goal_result.result == result
    assert goal_result.latest_state == latest_state
    assert goal_result.status == "completed"
    assert calls == [
        (
            harness,
            "inspect page",
            session_config,
            {"configurable": {"thread_id": "session-1"}},
        )
    ]
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, result)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert [record.task_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[0].payload == {"task": "inspect page"}
    assert sink.records[1].payload == {"task": "inspect page", "result": result}


@pytest.mark.asyncio
async def test_goal_runner_accepts_streaming_task_runner_unchanged() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []

    class StreamingHarness:
        def __init__(self) -> None:
            self.streamed = False
            self.ran = False

        async def stream_updates(self, task: str, config: dict[str, Any]):
            self.streamed = True
            yield {"plan": {"task": task}}
            yield {"agent": {"final_answer": f"streamed: {task}", "config": config}}

        async def run(self, task: str, config: dict[str, Any]):
            self.ran = True
            return {"final_answer": f"run: {task}", "config": config}

    harness = StreamingHarness()

    async def streaming_task_runner(
        call_harness: Any,
        task: str,
        _config: Any,
        task_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        final_update: dict[str, Any] | None = None
        async for chunk in call_harness.stream_updates(task, config=task_config):
            final_update = chunk
        return final_update

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return {"state": fallback}

    runner = GoalRunner(
        harness=harness,
        session_config=object(),
        task_runner=streaming_task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=DictResultCompiler(),
    )

    goal_result = await runner.run(make_request())

    expected_result = {
        "agent": {
            "final_answer": "streamed: inspect page",
            "config": {"configurable": {"thread_id": "session-1"}},
        }
    }
    assert goal_result.status == "completed"
    assert goal_result.result == expected_result
    assert goal_result.latest_state == {"state": expected_result}
    assert harness.streamed is True
    assert harness.ran is False
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, expected_result)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]
    assert sink.records[1].payload == {"task": "inspect page", "result": expected_result}


@pytest.mark.asyncio
async def test_goal_runner_emits_failure_event_and_reraises_original_exception() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []
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

    runner = GoalRunner(
        harness=object(),
        session_config=object(),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=DictResultCompiler(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await runner.run(make_request())

    assert exc_info.value is error
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, None)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert [record.goal_id for record in sink.records] == ["task-1", "task-1"]
    assert [record.task_id for record in sink.records] == ["task-1", "task-1"]
    assert sink.records[1].payload == {"task": "inspect page", "error": error}


@pytest.mark.asyncio
async def test_goal_runner_times_out_and_emits_failure_event() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        return {"final_answer": "never reached"}

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return {"decision": "tool_call", "observation": "waiting"}

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(goal_timeout_seconds=0.01),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
    )

    with pytest.raises(TimeoutError, match="timed out after 0.01"):
        await runner.run(make_request())

    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, None)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert sink.records[1].payload["task"] == "inspect page"
    assert isinstance(sink.records[1].payload["error"], TimeoutError)


@pytest.mark.asyncio
async def test_goal_runner_watchdog_fails_when_events_stop() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []
    cancelled = False

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return {"final_answer": "never reached"}

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return {"decision": "tool_call", "observation": "stalled"}

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(
            goal_timeout_seconds=1,
            goal_watchdog_seconds=0.01,
        ),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
    )

    with pytest.raises(TimeoutError, match="made no progress for 0.01"):
        await runner.run(make_request())

    assert cancelled is True
    assert state_calls == [({"configurable": {"thread_id": "session-1"}}, None)]
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]
    assert isinstance(sink.records[1].payload["error"], TimeoutError)


@pytest.mark.asyncio
async def test_goal_runner_uses_terminal_result_when_latest_state_loader_stalls() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    result = {"final_answer": "done"}

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        return result

    async def latest_state_loader(
        _task_config: dict[str, Any],
        _fallback: Any | None,
    ) -> dict[str, object] | None:
        await asyncio.Event().wait()
        return None

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(
            goal_timeout_seconds=1,
            latest_state_timeout_seconds=0.01,
        ),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=DictResultCompiler(),
    )

    goal_result = await runner.run(make_request())

    assert goal_result.status == "completed"
    assert goal_result.result == result
    assert goal_result.latest_state == result
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]


@pytest.mark.asyncio
async def test_goal_runner_rejects_nonterminal_result() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    state_calls: list[tuple[dict[str, Any], Any | None]] = []

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        return {"agent": {"observation": "compiled"}}

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return {"agent": {"observation": "compiled"}}

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(goal_timeout_seconds=1),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=DictResultCompiler(),
    )

    with pytest.raises(RuntimeError, match="without a terminal goal state"):
        await runner.run(make_request())

    assert state_calls == [
        (
            {"configurable": {"thread_id": "session-1"}},
            {"agent": {"observation": "compiled"}},
        )
    ]
    assert [record.type for record in sink.records] == ["goal.started", "goal.failed"]


@pytest.mark.asyncio
async def test_goal_runner_emits_blocked_event_for_blocked_final_answer() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        return {"agent": {"final_answer": "Blocked: repeated recovery attempts"}}

    async def latest_state_loader(
        _task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        return fallback

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(goal_timeout_seconds=1),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=DictResultCompiler(),
    )

    goal_result = await runner.run(make_request())

    assert goal_result.status == "blocked"
    assert [record.type for record in sink.records] == ["goal.started", "goal.blocked"]


@pytest.mark.asyncio
async def test_goal_runner_uses_compiled_goal_state_without_agent_state() -> None:
    sink = InMemoryEventSink()
    event_emitter = EventEmitter(sink, session_id="session-1")
    result = {"engine": {"answer": "done"}}
    latest_state = {"engine": {"answer": "done"}}

    class IndependentCompiler:
        def compile(
            self,
            *,
            latest_state: dict[str, object] | None,
            result: Any,
        ) -> GoalState:
            return GoalState(status="done", latest_state=latest_state, result=result)

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> dict[str, Any]:
        return result

    async def latest_state_loader(
        _task_config: dict[str, Any],
        _fallback: Any | None,
    ) -> dict[str, object] | None:
        return latest_state

    runner = GoalRunner(
        harness=object(),
        session_config=SimpleNamespace(goal_timeout_seconds=1),
        task_runner=task_runner,
        event_emitter=event_emitter,
        latest_state_loader=latest_state_loader,
        observation_compiler=IndependentCompiler(),
    )

    goal_result = await runner.run(make_request())

    assert goal_result.status == "completed"
    assert goal_result.result == result
    assert goal_result.latest_state == latest_state
    assert [record.type for record in sink.records] == ["goal.started", "goal.completed"]
