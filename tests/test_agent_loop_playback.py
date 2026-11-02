"""The engine's step-source branch: playback proposes steps before the model is asked."""

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
from src.config import PermissionRule, PermissionsSettings, ReplaySettings, Settings
from src.contracts import (
    HookEvent,
    HookResult,
    ModelCallRecord,
    PlaybackRequest,
    RunProfile,
    StepFinal,
    StepHandoff,
    StepProposal,
    StepView,
    Tool,
)
from src.harness.hooks import HookEngine, RegisteredHook
from src.harness.permissions import PermissionEngine
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelResponse

RECORDED_PLAN = [
    {"id": 1, "description": "Open the page", "status": "pending"},
    {"id": 2, "description": "Read the price", "status": "pending"},
]
PLAN = {"steps": [{"id": 1, "description": "Plan from the model", "status": "pending"}]}
DONE = {"decision": "done", "final_answer": "Price is 42."}


class CountingModel(FakeChatModel):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__([json.dumps(response) for response in responses])
        self.calls = 0
        self.seen: list[Any] = []

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        self.seen.append(list(messages))
        content = self._responses[min(self.calls, len(self._responses)) - 1]
        return ModelResponse(content=content, finish_reason="stop")


@dataclass
class ScriptedSource:
    """A step source that returns scripted outcomes and records what it observed."""

    outcomes: list[Any]
    recorded_plan: list[dict[str, Any]] | None = field(default_factory=lambda: list(RECORDED_PLAN))
    observed: list[tuple[int, dict[str, Any], str]] = field(default_factory=list)
    views: list[StepView] = field(default_factory=list)
    info: Mapping[str, Any] = field(default_factory=lambda: {"recording": "r", "steps": 2, "answer": "agent"})
    usage: Sequence[ModelCallRecord] = ()

    def plan(self) -> list[Any] | None:
        return self.recorded_plan

    async def next(self, view: StepView) -> Any:
        self.views.append(view)
        return self.outcomes.pop(0) if self.outcomes else StepHandoff(reason="No more steps.", kind="exhausted")

    def observe(self, proposal: StepProposal, result: Mapping[str, Any], view: StepView, *, blocked: str = "") -> None:
        self.observed.append((proposal.step, dict(result), blocked))


@dataclass
class Factory:
    source: ScriptedSource
    built: list[dict[str, Any]] = field(default_factory=list)

    def build(self, request: PlaybackRequest, *, task: str, budget: Any, max_stage: str, emit: Any) -> ScriptedSource:
        self.built.append({"request": request, "task": task, "max_stage": max_stage})
        return self.source


class Shop:
    """Two tools: a read-only page view and a state-changing open."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tools(self) -> list[Tool]:
        async def view_page() -> str:
            self.calls.append(("view_page", {}))
            return "price 42"

        async def open_page(url: str) -> str:
            self.calls.append(("open_page", {"url": url}))
            return f"opened {url}"

        schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
        return [
            Tool(name="view_page", func=view_page, annotations={"readOnlyHint": True}),
            Tool(name="open_page", func=open_page, input_schema=schema),
        ]


def propose(step: int, name: str, plan_step: int | None = None, **args: Any) -> StepProposal:
    return StepProposal(request={"name": name, "args": args}, stage="playback", step=step, plan_step=plan_step)


async def run(
    source: ScriptedSource | None,
    responses: list[dict[str, Any]],
    *,
    profile: RunProfile | None = None,
    permissions: PermissionEngine | None = None,
    hooks: Any = None,
    human_input: Any = None,
    turn_cap: int = 10,
    shop: Shop | None = None,
) -> tuple[AgentLoopResult, list[EventRecord], CountingModel, Shop]:
    shop = shop or Shop()
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    llm = CountingModel(responses)
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=shop.tools()), event_emitter=emitter)
    resources = EngineResources.from_harness(
        harness,
        llm=llm,
        events=emitter,
        hooks=hooks,
        permissions=permissions,
        steps=Factory(source) if source is not None else None,
    )
    if profile is None:
        profile = RunProfile(playback=PlaybackRequest(recording="r"))
    result = await AgentLoopEngine(resources, human_input=human_input).run(
        "Read the price.", task_id="task-1", session_id="session-1", turn_cap=turn_cap, profile=profile
    )
    return result, list(sink.records), llm, shop


def types(records: list[EventRecord]) -> list[str]:
    return [record.type for record in records]


def of(records: list[EventRecord], kind: str) -> list[dict[str, Any]]:
    return [dict(record.payload) for record in records if record.type == kind]


# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replay_that_finishes_itself_never_calls_the_model() -> None:
    source = ScriptedSource(
        [propose(1, "open_page", 0, url="https://shop.example"), propose(2, "view_page", 1), StepFinal("price 42")]
    )

    result, records, llm, shop = await run(source, [PLAN, DONE])

    assert llm.calls == 0
    assert result.status == "done"
    assert result.stop_reason == "playback_completed"
    assert result.final_answer == "price 42"
    assert shop.calls == [("open_page", {"url": "https://shop.example"}), ("view_page", {})]
    assert result.state.usage.stage_reached == "playback"
    assert of(records, "playback.started") == [{"recording": "r", "steps": 2, "answer": "agent"}]
    assert [payload["source"] for payload in of(records, "action.proposed")] == ["playback", "playback"]
    assert [(p["step"], p["status"]) for p in of(records, "playback.step")] == [(1, "success"), (2, "success")]
    assert "model.requested" not in types(records)


@pytest.mark.asyncio
async def test_the_recorded_plan_replaces_the_planner_and_steps_move_it() -> None:
    source = ScriptedSource([propose(1, "open_page", 0, url="https://a.example"), propose(2, "view_page", 1), StepFinal("ok")])

    result, records, _, _ = await run(source, [PLAN, DONE])

    plans = of(records, "plan.updated")
    assert [step["description"] for step in plans[0]["steps"]] == ["Open the page", "Read the price"]
    assert [plan["current_step"] for plan in plans] == [0, 1, 2]
    assert result.state.replan_count == 0


@pytest.mark.asyncio
async def test_the_source_sees_each_step_result() -> None:
    source = ScriptedSource([propose(1, "view_page"), StepFinal("ok")])

    await run(source, [PLAN])

    ((step, result, blocked),) = source.observed
    assert (step, blocked) == (1, "")
    assert (result["name"], result["status"], result["content"]) == ("view_page", "success", "price 42")
    # The second view already shows the replayed step's output.
    assert source.views[1].tool_result["content"] == "price 42"
    assert source.views[1].tool_schemas["view_page"]["read_only"] is True


@pytest.mark.asyncio
async def test_a_handoff_gives_the_model_the_history_and_a_harness_note() -> None:
    source = ScriptedSource(
        [propose(1, "view_page"), StepHandoff(reason="The page changed; continue yourself.", kind="checkpoint", step=1, detail="path differs")]
    )

    result, records, llm, _ = await run(source, [DONE])

    assert llm.calls == 1  # no planner call: the recorded plan stays
    assert result.status == "done" and result.final_answer == "Price is 42."
    assert result.state.usage.stage_reached == "agent"
    assert of(records, "playback.diverged") == [{"step": 1, "kind": "checkpoint", "detail": "path differs"}]
    (escalation,) = of(records, "ladder.escalated")
    assert escalation == {"from": "playback", "to": "agent", "reason": "checkpoint", "step": 1}
    contents = [str(getattr(message, "content", "")) for message in llm.seen[0]]
    assert any("price 42" in content for content in contents)  # the replayed tool output
    assert any(content.startswith("[harness] The page changed") for content in contents)


@pytest.mark.asyncio
async def test_a_denied_step_is_not_terminal_the_source_hands_over() -> None:
    deny = PermissionEngine.from_settings(
        PermissionsSettings(rules=[PermissionRule(id="no-open", decision="deny", tool="open_page")])
    )
    source = ScriptedSource(
        [propose(1, "open_page", url="https://x.example"), StepHandoff(reason="A rule denied the step.", kind="denied", step=1)]
    )

    result, records, llm, shop = await run(source, [DONE], permissions=deny)

    assert shop.calls == []
    ((step, outcome, blocked),) = source.observed
    assert step == 1 and outcome == {} and blocked
    assert of(records, "playback.step")[0]["status"] == "blocked"
    assert result.status == "done" and llm.calls == 1


@pytest.mark.asyncio
async def test_a_refused_approval_on_a_replayed_step_is_terminal() -> None:
    ask = PermissionEngine.from_settings(
        PermissionsSettings(rules=[PermissionRule(id="ask-open", decision="ask", tool="open_page")])
    )

    async def refuse(*_args: Any) -> str:
        return "deny"

    source = ScriptedSource([propose(1, "open_page", url="https://x.example")])

    result, _, llm, shop = await run(source, [DONE], permissions=ask, human_input=refuse)

    assert result.status == "blocked" and result.stop_reason == "approval_denied"
    assert shop.calls == [] and llm.calls == 0
    assert source.observed == []


@pytest.mark.asyncio
async def test_a_run_capped_at_playback_is_blocked_instead_of_handed_over() -> None:
    source = ScriptedSource([StepHandoff(reason="Element missing.", kind="binding", step=1)])
    profile = RunProfile(playback=PlaybackRequest(recording="r"), max_stage="playback")

    result, records, llm, _ = await run(source, [DONE], profile=profile)

    assert result.status == "blocked" and result.stop_reason == "ladder_cap"
    assert llm.calls == 0
    assert "ladder.escalated" not in types(records)
    assert of(records, "playback.diverged")[0]["kind"] == "binding"


@pytest.mark.asyncio
async def test_replayed_steps_do_not_use_up_the_agent_turn_cap() -> None:
    steps = [propose(index, "open_page", 0, url=f"https://x.example/{index}") for index in range(1, 11)]
    source = ScriptedSource([*steps, StepFinal("all ten")])

    result, _, llm, shop = await run(source, [DONE], turn_cap=2)

    assert result.status == "done" and result.final_answer == "all ten"
    assert len(shop.calls) == 10 and llm.calls == 0
    assert result.turns == 11


@pytest.mark.asyncio
async def test_a_repeated_replayed_call_hands_over_instead_of_replanning() -> None:
    same = [propose(index, "open_page", url="https://same.example") for index in (1, 2, 3)]
    source = ScriptedSource(same)

    result, records, llm, shop = await run(source, [DONE])

    assert len(shop.calls) == 2  # the third identical call never runs
    assert of(records, "playback.diverged")[0]["kind"] == "repeat"
    assert llm.calls == 1 and result.status == "done"
    assert result.state.replan_count == 0


@pytest.mark.asyncio
async def test_a_stop_hook_that_rejects_the_replayed_answer_hands_over() -> None:
    async def reject(event: HookEvent) -> HookResult:
        if event.final_answer == "stale":
            return HookResult(decision="deny", reason="the answer has no price")
        return HookResult()

    hooks = HookEngine([RegisteredHook(id="grounded", event="stop", handler=reject)])  # type: ignore[arg-type]
    source = ScriptedSource([StepFinal("stale")])

    result, records, llm, _ = await run(source, [DONE], hooks=hooks)

    assert result.final_answer == "Price is 42." and llm.calls == 1
    assert of(records, "ladder.escalated")[0]["reason"] == "answer"
    contents = [str(getattr(message, "content", "")) for message in llm.seen[0]]
    assert any("Completion rejected: the answer has no price" in content for content in contents)


@pytest.mark.asyncio
async def test_a_source_without_a_plan_lets_the_model_plan() -> None:
    source = ScriptedSource([StepFinal("ok")], recorded_plan=None)

    result, _, llm, _ = await run(source, [PLAN, DONE])

    assert llm.calls == 1 and result.final_answer == "ok"


@pytest.mark.asyncio
async def test_a_runaway_source_is_stopped_by_the_recording_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(replay=ReplaySettings(max_steps=2, max_retargets_per_run=0))
    monkeypatch.setattr("src.agent_loop.execution.loop.get_settings", lambda: settings)
    endless = [propose(index, "open_page", url=f"https://x.example/{index}") for index in range(1, 50)]
    source = ScriptedSource(endless)

    result, records, llm, shop = await run(source, [DONE])

    assert len(shop.calls) == 3
    assert of(records, "playback.diverged")[0]["kind"] == "exhausted"
    assert llm.calls == 1 and result.status == "done"


@pytest.mark.asyncio
async def test_playback_without_a_step_source_fails_the_task() -> None:
    with pytest.raises(ValueError, match="replay is not enabled"):
        await run(None, [PLAN, DONE])


@pytest.mark.asyncio
async def test_without_a_playback_profile_the_run_is_unchanged() -> None:
    """Default profile: no source is built and the event stream matches a run without one."""

    source = ScriptedSource([StepFinal("never")])
    responses = [PLAN, {"decision": "tool_call", "tool_request": {"name": "view_page", "args": {}, "id": "c1"}}, DONE]

    with_factory, records_a, llm_a, _ = await run(source, responses, profile=RunProfile())
    without, records_b, llm_b, _ = await run(None, responses, profile=RunProfile())

    assert source.views == []
    assert llm_a.calls == llm_b.calls == 3
    assert types(records_a) == types(records_b)
    assert [r.payload for r in records_a if r.type != "turn.started"] == [
        r.payload for r in records_b if r.type != "turn.started"
    ]
    assert with_factory.state.usage.stage_reached == without.state.usage.stage_reached == "agent"
