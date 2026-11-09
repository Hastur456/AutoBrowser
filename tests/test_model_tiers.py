"""The model cascade in the engine: tiers, escalation, events and usage per tier."""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.loop import AgentLoopEngine, AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.config import get_settings
from src.contracts import RunBudget, RunProfile, Tool
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelPrice, ModelResponse, ModelTier, ModelTiers, ModelUsage

PLAN = json.dumps({"steps": [{"id": 1, "description": "Look", "status": "pending"}]})
LOOK = json.dumps({"decision": "tool_call", "tool_request": {"name": "look", "args": {}, "id": "c1"}})
DONE = json.dumps({"decision": "done", "final_answer": "Seen."})
REPLAN = json.dumps({"decision": "replan", "reason": "The plan is wrong."})
EMPTY = ""

pytestmark = pytest.mark.asyncio


class Scripted:
    """Answers its script in order, then repeats the last answer; counts its calls."""

    def __init__(self, *responses: str, tokens: tuple[int, int] = (1000, 100)) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.tokens = tokens

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        content = self.responses[min(self.calls, len(self.responses)) - 1]
        usage = ModelUsage(input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        return ModelResponse(content=content, finish_reason="stop", usage=usage)


async def look(q: str = "") -> str:
    return f"a page {q}"


def look_at(q: str) -> str:
    return json.dumps({"decision": "tool_call", "tool_request": {"name": "look", "args": {"q": q}, "id": q}})


#: Alternating calls dodge the consecutive-repeat replan; the 4th identical one is blocked by
#: the progress guard (``loop.max_ineffective_actions`` = 3).
ALTERNATING = [look_at(q) for q in "ababab"] + [look_at("a")]


async def run(
    models: ModelTiers | None,
    llm: Any,
    *,
    profile: RunProfile | None = None,
) -> tuple[AgentLoopResult, list[EventRecord]]:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="s")
    tools = ToolRegistry(tools=[Tool(name="look", func=look)])
    harness = BrowserHarness(llm=llm, tool_registry=tools, event_emitter=emitter)
    resources = EngineResources.from_harness(harness, llm=llm, events=emitter, models=models)
    result = await AgentLoopEngine(resources).run(
        "Look.", task_id="t", session_id="s", turn_cap=20, profile=profile
    )
    return result, list(sink.records)


def two_tiers(cheap: Scripted, strong: Scripted, *, prices: bool = False) -> ModelTiers:
    return ModelTiers(
        (
            ModelTier(cheap, "cheap", ModelPrice(1.0, 1.0) if prices else None),
            ModelTier(strong, "strong", ModelPrice(10.0, 10.0) if prices else None),
        )
    )


def of_type(records: list[EventRecord], event_type: str) -> list[dict[str, Any]]:
    return [dict(record.payload) for record in records if record.type == event_type]


# -- escalation reasons ---------------------------------------------------------------------


@pytest.fixture
def cascade(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set ``cascade.*`` fields through the environment for one test."""

    def setter(**fields: Any) -> None:
        for name, value in fields.items():
            monkeypatch.setenv(f"AUTOBROWSER_CASCADE__{name.upper()}", str(value).lower())
        get_settings.cache_clear()

    yield setter
    get_settings.cache_clear()


async def test_a_progress_block_moves_the_next_turn_to_the_strong_tier(cascade: Any) -> None:
    cheap, strong = Scripted(PLAN, *ALTERNATING), Scripted(DONE)

    result, records = await run(two_tiers(cheap, strong), cheap)

    assert result.status == "done"
    assert result.final_answer == "Seen."
    assert strong.calls == 1
    escalated = of_type(records, "ladder.escalated")
    assert escalated == [
        {"from": "agent", "to": "agent", "reason": "progress_block", "from_tier": 0, "tier": 1, "model": "strong"}
    ]
    assert result.state.model_tier == 1


async def test_the_planner_runs_on_tier_0_and_a_replan_on_the_current_tier() -> None:
    cheap, strong = Scripted(PLAN, REPLAN, PLAN, DONE), Scripted(DONE)

    result, records = await run(two_tiers(cheap, strong), cheap)

    assert result.status == "done"
    # Call #0, the replan request and the replanned plan are the cheap model's; the turn
    # after the replan runs on the strong tier.
    assert cheap.calls == 3
    assert strong.calls == 1
    assert [event["reason"] for event in of_type(records, "ladder.escalated")] == ["replans"]
    plan_calls = [event for event in of_type(records, "model.requested") if event["phase"] == "plan"]
    assert [(event["tier"], event["model"]) for event in plan_calls] == [(0, "cheap"), (0, "cheap")]


async def test_an_empty_answer_goes_up_a_tier_instead_of_ending_the_task() -> None:
    cheap, strong = Scripted(PLAN, EMPTY), Scripted(DONE)

    result, records = await run(two_tiers(cheap, strong), cheap)

    assert result.status == "done"
    assert [event["reason"] for event in of_type(records, "ladder.escalated")] == ["no_action"]


async def test_an_empty_answer_on_the_top_tier_still_ends_blocked() -> None:
    cheap, strong = Scripted(PLAN, EMPTY), Scripted(EMPTY)

    result, _ = await run(two_tiers(cheap, strong), cheap)

    assert result.status == "blocked"
    assert strong.calls == 1


async def test_an_empty_answer_without_a_cascade_ends_blocked_as_before() -> None:
    cheap = Scripted(PLAN, EMPTY)

    result, records = await run(None, cheap)

    assert result.status == "blocked"
    assert of_type(records, "ladder.escalated") == []


async def test_the_new_tier_gets_a_fresh_failure_window(cascade: Any) -> None:
    async def broken() -> str:
        raise RuntimeError("boom")

    cascade(escalate_on_progress_block=False, escalate_after_replans=50)
    fail = json.dumps({"decision": "tool_call", "tool_request": {"name": "broken", "args": {}, "id": "c"}})
    cheap, strong = Scripted(PLAN, fail), Scripted(fail, fail, DONE)
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="s")
    tools = ToolRegistry(tools=[Tool(name="broken", func=broken)])
    harness = BrowserHarness(llm=cheap, tool_registry=tools, event_emitter=emitter)
    resources = EngineResources.from_harness(harness, llm=cheap, events=emitter, models=two_tiers(cheap, strong))
    result = await AgentLoopEngine(resources).run("Fix.", task_id="t", session_id="s", turn_cap=20)

    # Two failures on the cheap tier escalate; the strong tier fails twice more and still
    # finishes, below loop.max_consecutive_failures (3) of its own.
    assert result.status == "done"
    assert (cheap.calls, strong.calls) == (3, 3)


# -- usage, budget and events ---------------------------------------------------------------


async def test_usage_is_split_by_tier_label_and_priced_per_tier() -> None:
    cheap, strong = Scripted(PLAN, LOOK), Scripted(DONE)

    result, _ = await run(two_tiers(cheap, strong, prices=True), cheap)

    usage = result.usage
    assert usage is not None
    assert usage.by_model["strong"]["model_calls"] == 1
    assert usage.by_model["cheap"]["model_calls"] == cheap.calls
    assert usage.by_model["strong"]["cost"] == pytest.approx((1000 + 100) * 10.0 / 1_000_000)


async def test_the_budget_is_checked_before_a_call_of_any_tier() -> None:
    cheap, strong = Scripted(PLAN, LOOK), Scripted(DONE)
    profile = RunProfile(budget=RunBudget(max_model_calls=5))

    result, _ = await run(two_tiers(cheap, strong), cheap, profile=profile)

    # Plan + 4 looks spend the budget before the strong tier gets a call.
    assert result.status == "blocked"
    assert result.stop_reason == "budget_exhausted"
    assert strong.calls == 0


async def test_model_events_carry_the_tier_only_with_several_tiers() -> None:
    cheap, strong = Scripted(PLAN, LOOK), Scripted(DONE)

    _, records = await run(two_tiers(cheap, strong), cheap)

    agent_calls = [event for event in of_type(records, "model.responded") if event["phase"] == "agent"]
    assert {(event["tier"], event["model"]) for event in agent_calls} == {(0, "cheap"), (1, "strong")}


def _shape(records: list[EventRecord]) -> list[tuple[str, dict[str, Any]]]:
    volatile = {"metadata"}
    return [
        (record.type, {key: value for key, value in dict(record.payload).items() if key not in volatile})
        for record in records
    ]


async def test_one_tier_keeps_the_event_stream_unchanged() -> None:
    """With one tier (no cascade) the stream has no tier keys and no escalation, and is the
    same whether the session passes its tier or not."""

    _, implicit = await run(None, Scripted(PLAN, LOOK, DONE))
    _, explicit = await run(ModelTiers.single(model := Scripted(PLAN, LOOK, DONE)), model)

    assert _shape(implicit) == _shape(explicit)
    for event_type, payload in _shape(implicit):
        assert event_type != "ladder.escalated"
        if event_type.startswith("model."):
            assert "tier" not in payload and "model" not in payload
