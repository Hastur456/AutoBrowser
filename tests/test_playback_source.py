"""``PlaybackSource``: stage 1 of the ladder over one recording (no model)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.recordings import RecordedStep, Recording
from src.browser.playback import BrowserPlaybackAdapter
from src.config import ReplaySettings
from src.contracts import (
    Binding,
    ModelCallRecord,
    PlaybackRequest,
    RunBudget,
    StepFinal,
    StepHandoff,
    StepProposal,
    StepView,
    TaskUsage,
)
from src.harness.playback import PlaybackFactory, PlaybackSource
from src.harness.recording_store import RecordingNotFoundError, RecordingStore

PAGE = """- Page URL: https://shop.example/item/1
- tablist:
  - tab "Specs" [ref=e51]
- button "Buy now" [ref=e52]"""
CATALOG = {
    "open": {"server": "web", "read_only": False, "schema_digest": "sha256:open"},
    "click": {"server": "web", "read_only": False, "schema_digest": "sha256:click"},
}
SPECS = Binding(path=("target",), value="e21", descriptor={"role": "tab", "name": "Specs", "nth": 1, "count": 1, "context": ["tablist"], "frame": ""})


def recording(*steps: RecordedStep, answer: str = "summary") -> Recording:
    return Recording(
        name="kettle",
        task="Read the specs",
        steps=steps,
        answer=answer,  # type: ignore[arg-type]
        plan=({"id": 1, "description": "Open", "status": "pending"},),
        catalog=CATALOG,
    )


OPEN = RecordedStep(index=1, tool="open", args={"url": "https://shop.example/item/1"}, server="web", plan_step=0)
CLICK = RecordedStep(index=2, tool="click", args={"element": "Specs", "target": "e21"}, server="web", bindings=(SPECS,))


def view(content: str = PAGE, *, usage: TaskUsage | None = None, schemas: dict | None = None) -> StepView:
    return StepView(
        task="t",
        turn=1,
        tool_result={"name": "open", "status": "success", "content": content},
        snapshots={},
        tool_schemas=CATALOG if schemas is None else schemas,
        usage=usage or TaskUsage(),
    )


class FakeRetargeter:
    def __init__(self, args: dict[str, Any] | None) -> None:
        self.args = args
        self.calls: list[str] = []

    async def retarget(self, *, task, step, reason, view, page_texts, emit):  # noqa: ANN001, ANN201
        self.calls.append(reason)
        return self.args, ModelCallRecord(phase="retarget", model="cheap", usage={"input_tokens": 10, "output_tokens": 2})


def source(rec: Recording, *, retargeter: Any = None, **kwargs: Any) -> PlaybackSource:
    return PlaybackSource(
        rec,
        PlaybackRequest(recording=rec.name),
        BrowserPlaybackAdapter(ReplaySettings()),
        kwargs.pop("settings", ReplaySettings()),
        retargeter=retargeter,
        **kwargs,
    )


def done(src: PlaybackSource, proposal: StepProposal, content: str = PAGE, status: str = "success") -> None:
    src.observe(proposal, {"status": status, "content": content, "error": "boom" if status != "success" else ""}, view(content))


@pytest.mark.asyncio
async def test_steps_replay_with_rebound_refs_then_a_summary() -> None:
    src = source(recording(OPEN, CLICK))

    first = await src.next(view())
    done(src, first)
    second = await src.next(view())
    done(src, second)
    final = await src.next(view())

    assert isinstance(first, StepProposal) and first.request == {"name": "open", "args": {"url": "https://shop.example/item/1"}}
    assert (first.step, first.plan_step, first.binding) == (1, 0, "none")
    assert isinstance(second, StepProposal) and second.request["args"]["target"] == "e51" and second.binding == "rebound"
    assert final == StepFinal("Replayed 2 recorded steps of kettle.", stage="playback")
    assert src.plan() == [{"id": 1, "description": "Open", "status": "pending"}]
    assert src.info == {"recording": "kettle", "steps": 2, "answer": "summary"}


@pytest.mark.asyncio
async def test_the_output_answer_is_the_region_of_the_last_output() -> None:
    src = PlaybackSource(
        recording(OPEN),
        PlaybackRequest(recording="kettle", answer="output", select=r'button "[^"]+"'),
        BrowserPlaybackAdapter(ReplaySettings()),
        ReplaySettings(),
    )
    done(src, await src.next(view()))

    assert await src.next(view()) == StepFinal('button "Buy now"', stage="playback")


@pytest.mark.asyncio
async def test_the_agent_answer_hands_over_after_the_last_step() -> None:
    src = source(recording(OPEN, answer="agent"))
    done(src, await src.next(view()))

    handoff = await src.next(view())

    assert isinstance(handoff, StepHandoff) and handoff.kind == "answer"


@pytest.mark.parametrize(
    "schemas, detail",
    [
        ({"open": CATALOG["open"]}, "tool not available"),
        ({**CATALOG, "click": {**CATALOG["click"], "schema_digest": "sha256:other"}}, "input schema changed"),
        ({**CATALOG, "click": {**CATALOG["click"], "read_only": True}}, "readOnlyHint changed"),
    ],
)
@pytest.mark.asyncio
async def test_a_changed_catalog_hands_over_before_the_call(schemas: dict, detail: str) -> None:
    src = source(recording(CLICK))

    handoff = await src.next(view(schemas=schemas))

    assert isinstance(handoff, StepHandoff)
    assert (handoff.kind, handoff.step, handoff.detail) == ("catalog", 2, detail)
    assert handoff.reason.startswith("Playback stopped:")


@pytest.mark.asyncio
async def test_a_redacted_step_is_never_replayed() -> None:
    secret = RecordedStep(index=1, tool="open", args={"password": "[REDACTED]"}, redacted=True)
    retargeter = FakeRetargeter({"password": "guess"})

    handoff = await source(recording(secret), retargeter=retargeter).next(view())

    assert isinstance(handoff, StepHandoff) and handoff.kind == "binding" and "secret" in handoff.reason
    assert retargeter.calls == []


@pytest.mark.asyncio
async def test_a_missing_element_without_a_retargeter_hands_over() -> None:
    handoff = await source(recording(CLICK)).next(view("- Page URL: https://shop.example/item/1"))

    assert isinstance(handoff, StepHandoff) and handoff.kind == "binding" and 'no tab "Specs"' in handoff.detail


@pytest.mark.asyncio
async def test_a_missing_element_goes_to_the_retargeter_first() -> None:
    retargeter = FakeRetargeter({"element": "Specs", "target": "e52"})
    events: list[tuple[str, dict]] = []
    src = source(recording(CLICK), retargeter=retargeter, emit=lambda kind, payload: events.append((kind, dict(payload))))

    proposal = await src.next(view("- Page URL: https://shop.example/item/1\n- tab \"Details\" [ref=e52]"))

    assert isinstance(proposal, StepProposal)
    assert (proposal.stage, proposal.binding, proposal.request["args"]["target"]) == ("retarget", "retargeted", "e52")
    assert [call.phase for call in src.usage] == ["retarget"]
    assert events == [("ladder.escalated", {"from": "playback", "to": "retarget", "reason": "binding", "step": 2})]


@pytest.mark.asyncio
async def test_a_rejected_retarget_hands_over() -> None:
    src = source(recording(CLICK), retargeter=FakeRetargeter(None))

    handoff = await src.next(view("- Page URL: x"))

    assert isinstance(handoff, StepHandoff) and handoff.kind == "binding"
    assert len(src.usage) == 1  # the model call still counts


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_stage": "playback"},
        {"settings": ReplaySettings(retarget_enabled=False)},
        {"settings": ReplaySettings(max_retargets_per_run=0)},
        {"budget": RunBudget(max_model_calls=1)},
    ],
)
@pytest.mark.asyncio
async def test_retargeting_respects_the_ladder_the_settings_and_the_budget(kwargs: dict) -> None:
    retargeter = FakeRetargeter({"target": "e52"})
    src = source(recording(CLICK), retargeter=retargeter, **kwargs)

    handoff = await src.next(view("- Page URL: x", usage=TaskUsage(model_calls=1)))

    assert isinstance(handoff, StepHandoff) and retargeter.calls == []


@pytest.mark.asyncio
async def test_a_failed_step_is_retargeted_once_then_handed_over() -> None:
    retargeter = FakeRetargeter({"element": "Specs", "target": "e51"})
    src = source(recording(CLICK), retargeter=retargeter)

    first = await src.next(view())
    done(src, first, status="error")
    retry = await src.next(view())
    done(src, retry, status="error")
    handoff = await src.next(view())

    assert isinstance(retry, StepProposal) and retry.stage == "retarget"
    assert isinstance(handoff, StepHandoff) and handoff.kind == "tool_error"
    assert retargeter.calls == ["boom"]


@pytest.mark.asyncio
async def test_a_blocked_step_hands_over_as_denied() -> None:
    src = source(recording(OPEN))
    proposal = await src.next(view())

    src.observe(proposal, {}, view(), blocked="Blocked by rule no-open")
    handoff = await src.next(view())

    assert isinstance(handoff, StepHandoff) and handoff.kind == "denied"
    assert "Blocked by rule no-open" in handoff.reason


@pytest.mark.asyncio
async def test_an_unexpected_page_after_a_step_hands_over_as_checkpoint() -> None:
    adapter = BrowserPlaybackAdapter(ReplaySettings())
    step = RecordedStep(index=1, tool="open", args={"url": "u"}, server="web", checkpoint=adapter.checkpoint(PAGE))
    src = source(recording(step))

    done(src, await src.next(view()), content="- Page URL: https://shop.example/login\n- button \"Sign in\" [ref=e1]")
    handoff = await src.next(view())

    assert isinstance(handoff, StepHandoff) and handoff.kind == "checkpoint" and handoff.detail == "similarity 0.00"


@pytest.mark.asyncio
async def test_the_factory_reports_the_divergence_of_the_last_replay_once(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings())
    store.save(recording(dataclasses.replace(CLICK, index=1)))
    factory = PlaybackFactory(store, BrowserPlaybackAdapter(), ReplaySettings())

    src = factory.build(PlaybackRequest(recording="kettle"), task="t", budget=RunBudget(), max_stage="human", emit=lambda *_: None)
    await src.next(view("- Page URL: x"))  # the Specs tab is gone

    assert factory.diverged("kettle") == "binding"
    assert factory.diverged("kettle") == ""  # consumed


@pytest.mark.asyncio
async def test_an_empty_output_region_hands_over_instead_of_an_empty_answer() -> None:
    src = PlaybackSource(
        recording(OPEN),
        PlaybackRequest(recording="kettle", answer="output", select="no such text"),
        BrowserPlaybackAdapter(ReplaySettings()),
        ReplaySettings(),
    )
    done(src, await src.next(view()))

    handoff = await src.next(view())

    assert isinstance(handoff, StepHandoff) and handoff.kind == "checkpoint"
    assert handoff.detail == "the answer region is empty"


def test_the_factory_loads_by_name_and_checks_regexes(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings())
    store.save(recording(OPEN))
    factory = PlaybackFactory(store, BrowserPlaybackAdapter(), ReplaySettings())
    build = lambda request: factory.build(request, task="t", budget=RunBudget(), max_stage="human", emit=lambda *_: None)  # noqa: E731

    assert build(PlaybackRequest(recording="kettle")).info["steps"] == 1
    with pytest.raises(RecordingNotFoundError):
        build(PlaybackRequest(recording="other"))
    with pytest.raises(ValueError, match="invalid playback regex"):
        build(PlaybackRequest(recording="kettle", select="(unclosed"))
