"""The engine publishes the plan's content and the start of every turn (``plan.updated``,
``turn.started``) so a client can show a live checklist and a turn counter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.evals import FakeChatModel
from src.agent_loop.events import (
    AgentTraceSink,
    EventEmitter,
    EventRecord,
    InMemoryEventSink,
)
from src.agent_loop.execution.loop import AgentLoopEngine, AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.harness.hooks import NullHookEngine
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelResponse, ModelUsage

PLAN = {
    "steps": [
        {"id": 1, "description": "Open the page", "status": "pending"},
        {"id": 2, "description": "Read the price", "status": "pending"},
    ]
}
DONE = {"decision": "done", "final_answer": "All done."}
REPLAN = {"decision": "replan", "reason": "The page changed."}


async def run_engine(responses: list[dict[str, Any]]) -> tuple[AgentLoopResult, list[EventRecord]]:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    llm = FakeChatModel([json.dumps(response) for response in responses])
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=[]), event_emitter=emitter)
    resources = EngineResources.from_harness(
        harness, llm=llm, events=emitter, hooks=NullHookEngine()
    )
    result = await AgentLoopEngine(resources).run(
        "Find the price.", task_id="task-1", goal_id="task-1", session_id="session-1"
    )
    return result, list(sink.records)


def of_type(records: list[EventRecord], event_type: str) -> list[EventRecord]:
    return [record for record in records if record.type == event_type]


@pytest.mark.asyncio
async def test_the_plan_is_published_after_planning_and_when_steps_complete() -> None:
    result, records = await run_engine([PLAN, DONE])

    assert result.status == "done"
    plans = of_type(records, "plan.updated")
    assert [plan.payload["replan_count"] for plan in plans] == [0, 0]
    assert [step["status"] for step in plans[0].payload["steps"]] == ["pending", "pending"]
    assert [step["description"] for step in plans[0].payload["steps"]] == [
        "Open the page",
        "Read the price",
    ]
    assert plans[0].payload["current_step"] == 0
    # A done marks every step completed: a client sees the final checklist.
    assert [step["status"] for step in plans[1].payload["steps"]] == ["completed", "completed"]
    assert plans[1].payload["current_step"] == 2
    assert all(plan.task_id == "task-1" for plan in plans)


@pytest.mark.asyncio
async def test_the_plan_event_follows_the_planner_response_and_precedes_the_first_turn() -> None:
    _, records = await run_engine([PLAN, DONE])

    types = [record.type for record in records]
    first_plan = types.index("plan.updated")
    assert types[first_plan - 1] == "model.responded"
    assert records[first_plan - 1].payload == {"phase": "plan"}
    assert types[first_plan + 1] == "turn.started"


@pytest.mark.asyncio
async def test_every_turn_is_announced_with_its_number_and_step() -> None:
    result, records = await run_engine([PLAN, REPLAN, PLAN, DONE])

    turns = of_type(records, "turn.started")
    assert [turn.payload for turn in turns] == [
        {"turn": 1, "current_step": 0},
        {"turn": 2, "current_step": 0},
    ]
    assert result.turns == 2
    assert [plan.payload["replan_count"] for plan in of_type(records, "plan.updated")][:2] == [0, 1]


def test_the_agent_trace_keeps_the_plan_but_not_the_turns(tmp_path: Path) -> None:
    trace_path = tmp_path / "agent_trace.jsonl"
    emitter = EventEmitter(AgentTraceSink(trace_path), session_id="session-1")

    emitter.emit(
        "plan.updated",
        source="test",
        payload={
            "steps": [{"id": 1, "description": "Open the page", "status": "pending"}],
            "current_step": 0,
            "replan_count": 1,
        },
    )
    emitter.emit("turn.started", source="test", payload={"turn": 1, "current_step": 0})

    trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(trace) == 1
    trace[0].pop("timestamp")
    assert trace[0] == {
        "type": "plan.updated",
        "steps": [{"description": "Open the page", "status": "pending"}],
        "replan_count": 1,
    }


class UsageModel:
    """Replies like ``FakeChatModel`` but reports token usage on every response."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = [json.dumps(response) for response in responses]

    async def complete(self, messages: Any, *, tools: Any = (), **params: Any) -> ModelResponse:
        return ModelResponse(
            content=self._responses.pop(0),
            usage=ModelUsage(input_tokens=100, output_tokens=0),
        )


@pytest.mark.asyncio
async def test_model_responded_carries_the_reported_usage() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    llm = UsageModel([PLAN, DONE])
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=[]), event_emitter=emitter)
    resources = EngineResources.from_harness(
        harness, llm=llm, events=emitter, hooks=NullHookEngine()
    )

    await AgentLoopEngine(resources).run(
        "Find the price.", task_id="task-1", goal_id="task-1", session_id="session-1"
    )

    responded = of_type(list(sink.records), "model.responded")
    plan, agent = responded[0].payload, responded[1].payload
    assert plan == {"phase": "plan", "usage": {"input_tokens": 100, "output_tokens": 0}}
    assert agent["phase"] == "agent"
    assert agent["usage"] == {"input_tokens": 100, "output_tokens": 0}


@pytest.mark.asyncio
async def test_without_reported_usage_the_events_carry_none() -> None:
    _, records = await run_engine([PLAN, DONE])

    responded = of_type(records, "model.responded")
    assert "usage" not in responded[0].payload
    assert "usage" not in responded[1].payload
