"""Task usage accounting and run budgets (``budget_exhausted`` before the next model call)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.agent_loop.evals import FakeChatModel
from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.loop import AgentLoopEngine, AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.usage import add_call, budget_exhausted, effective_budget
from src.agent_loop.goals import GoalLimits, GoalRunner
from src.config import LoopSettings, Settings
from src.contracts import (
    ModelCallRecord,
    PlaybackRequest,
    RunBudget,
    RunProfile,
    StepFinal,
    StepProposal,
    StepView,
    TaskRequest,
    TaskUsage,
    Tool,
)
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelPrice, ModelResponse, ModelTiers, ModelUsage

PLAN = {"steps": [{"id": 1, "description": "Look", "status": "pending"}]}
LOOK = {"decision": "tool_call", "tool_request": {"name": "look", "args": {}, "id": "c1"}}
DONE = {"decision": "done", "final_answer": "Seen."}
PRICE = ModelPrice(input_per_mtok=1.0, output_per_mtok=4.0)


class Model(FakeChatModel):
    """Repeats its last response; reports usage unless ``tokens`` is ``None``."""

    def __init__(self, responses: list[dict[str, Any]], tokens: tuple[int, int] | None = (1000, 100)) -> None:
        super().__init__([json.dumps(response) for response in responses])
        self.calls = 0
        self.tokens = tokens

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        content = self._responses[min(self.calls, len(self._responses)) - 1]
        usage = ModelUsage(input_tokens=self.tokens[0], output_tokens=self.tokens[1]) if self.tokens else None
        return ModelResponse(content=content, finish_reason="stop", usage=usage)


async def look() -> str:
    return "a page"


async def run(
    model: Model,
    *,
    profile: RunProfile | None = None,
    price: ModelPrice | None = None,
    steps: Any = None,
) -> tuple[AgentLoopResult, list[EventRecord]]:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="s")
    harness = BrowserHarness(llm=model, tool_registry=ToolRegistry(tools=[Tool(name="look", func=look)]), event_emitter=emitter)
    resources = EngineResources.from_harness(
        harness,
        llm=model,
        events=emitter,
        steps=steps,
        models=ModelTiers.single(model, label="m-1", price=price),
    )
    result = await AgentLoopEngine(resources).run("Look.", task_id="t", session_id="s", turn_cap=20, profile=profile)
    return result, list(sink.records)


def budget(**caps: Any) -> RunProfile:
    return RunProfile(budget=RunBudget(**caps))


# -- pure accounting ------------------------------------------------------------------------


def test_calls_add_up_per_stage_and_model() -> None:
    usage = add_call(TaskUsage(), stage="agent", model="m", tokens={"input_tokens": 10, "output_tokens": 2}, cost=0.5)
    usage = add_call(usage, stage="retarget", model="cheap", tokens={"input_tokens": 5, "output_tokens": 1}, cost=0.1)

    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 15, 3)
    assert usage.cost == pytest.approx(0.6)
    assert usage.by_stage["retarget"] == {"model_calls": 1, "input_tokens": 5, "output_tokens": 1, "cost": 0.1}
    assert set(usage.by_model) == {"m", "cheap"}


def test_one_unknown_cost_makes_the_total_and_its_bucket_unknown() -> None:
    usage = add_call(TaskUsage(), stage="agent", model="m", tokens={}, cost=0.5)
    usage = add_call(usage, stage="agent", model="m", tokens={}, cost=None)
    usage = add_call(usage, stage="agent", model="m", tokens={}, cost=0.5)

    assert usage.cost is None
    assert "cost" not in usage.by_stage["agent"]


def test_the_profile_budget_wins_over_the_settings() -> None:
    loop = LoopSettings(max_task_model_calls=10, max_task_tokens=500)

    assert effective_budget(RunBudget(max_model_calls=2), loop) == RunBudget(max_model_calls=2, max_tokens=500)
    assert effective_budget(RunBudget(), LoopSettings()) == RunBudget()


def test_budget_exhausted_reasons() -> None:
    usage = TaskUsage(model_calls=2, input_tokens=90, output_tokens=10, cost=0.2)

    assert budget_exhausted(usage, RunBudget()) == ""
    assert "2 model calls" in budget_exhausted(usage, RunBudget(max_model_calls=2))
    assert "100 of its 100 tokens" in budget_exhausted(usage, RunBudget(max_tokens=100))
    assert budget_exhausted(usage, RunBudget(max_cost=0.2))
    assert budget_exhausted(TaskUsage(model_calls=2, cost=None), RunBudget(max_cost=0.01)) == ""


# -- the engine ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_budget_the_usage_is_reported() -> None:
    result, _ = await run(Model([PLAN, LOOK, DONE]), price=PRICE)

    usage = result.usage
    assert usage is not None and usage is result.state.usage
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (3, 3000, 300)
    assert usage.cost == pytest.approx(3 * (1000 * 1.0 + 100 * 4.0) / 1_000_000)
    assert usage.by_model["m-1"]["model_calls"] == 3
    assert usage.stage_reached == "agent"


@pytest.mark.asyncio
async def test_the_call_budget_blocks_before_the_next_call() -> None:
    model = Model([PLAN, LOOK, LOOK, DONE])

    result, records = await run(model, profile=budget(max_model_calls=2))

    assert model.calls == 2
    assert result.status == "blocked" and result.stop_reason == "budget_exhausted"
    assert "2 model calls" in result.final_answer
    requested = [r for r in records if r.type == "model.requested"]
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_the_token_budget_blocks() -> None:
    model = Model([PLAN, LOOK, LOOK, DONE], tokens=(600, 0))

    result, _ = await run(model, profile=budget(max_tokens=1000))

    assert model.calls == 2 and result.stop_reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_the_money_budget_needs_a_price() -> None:
    priced = Model([PLAN, LOOK, LOOK, LOOK, DONE], tokens=(1_000_000, 0))
    unpriced = Model([PLAN, LOOK, LOOK, LOOK, DONE], tokens=(1_000_000, 0))

    blocked, _ = await run(priced, profile=budget(max_cost=1.5), price=PRICE)
    free, _ = await run(unpriced, profile=budget(max_cost=1.5))

    assert priced.calls == 2 and blocked.stop_reason == "budget_exhausted"
    assert free.status == "done" and free.usage is not None and free.usage.cost is None


@pytest.mark.asyncio
async def test_a_replan_is_a_model_call_and_is_checked_too() -> None:
    model = Model([PLAN, {"decision": "replan", "reason": "changed"}, PLAN, DONE])

    result, records = await run(model, profile=budget(max_model_calls=2))

    assert model.calls == 2 and result.stop_reason == "budget_exhausted"
    assert [r.payload["phase"] for r in records if r.type == "model.requested"] == ["plan", "agent"]


@pytest.mark.asyncio
async def test_the_settings_budget_applies_without_a_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(loop=LoopSettings(max_task_model_calls=1))
    monkeypatch.setattr("src.agent_loop.execution.loop.get_settings", lambda: settings)
    model = Model([PLAN, LOOK, DONE])

    result, _ = await run(model)

    assert model.calls == 1 and result.stop_reason == "budget_exhausted"


@dataclass
class Source:
    outcomes: list[Any]
    usage: Sequence[ModelCallRecord] = ()
    info: Mapping[str, Any] = field(default_factory=lambda: {"recording": "r", "steps": 1, "answer": "output"})

    def plan(self) -> list[Any]:
        return [{"id": 1, "description": "Look", "status": "pending"}]

    async def next(self, view: StepView) -> Any:
        return self.outcomes.pop(0)

    def observe(self, proposal: Any, result: Any, view: Any, *, blocked: str = "") -> None:
        # A retarget made one cheap model call while proposing this step.
        self.usage = (ModelCallRecord(phase="retarget", model="cheap", usage={"input_tokens": 50, "output_tokens": 5}, cost=0.001),)


@dataclass
class Factory:
    source: Source

    def build(self, request: Any, **kwargs: Any) -> Source:
        return self.source


@pytest.mark.asyncio
async def test_replayed_steps_are_free_and_retargets_are_counted() -> None:
    proposal = StepProposal(request={"name": "look", "args": {}}, stage="retarget", step=1)
    source = Source([proposal, StepFinal("a page", stage="retarget")])
    model = Model([DONE])

    result, _ = await run(
        model,
        profile=RunProfile(playback=PlaybackRequest(recording="r"), budget=RunBudget(max_model_calls=1)),
        steps=Factory(source),
    )

    assert model.calls == 0 and result.status == "done"
    usage = result.usage
    assert usage is not None
    assert usage.model_calls == 1 and usage.by_stage["retarget"]["input_tokens"] == 50
    assert usage.stage_reached == "retarget"


@pytest.mark.asyncio
async def test_goal_events_carry_the_usage() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="s")

    async def task_runner(request: TaskRequest) -> AgentLoopResult:
        result, _ = await run(Model([PLAN, DONE]))
        return result

    await GoalRunner(task_runner=task_runner, events=emitter, limits=GoalLimits.from_settings(LoopSettings())).run(
        TaskRequest(task="Look.", task_id="t")
    )

    (completed,) = [r for r in sink.records if r.type == "goal.completed"]
    assert completed.payload["usage"]["model_calls"] == 2
    assert completed.payload["stage_reached"] == "agent"
