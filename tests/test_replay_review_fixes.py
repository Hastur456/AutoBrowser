"""Regression tests for the review of the replay batch (recorder start state, turn cap on a
handoff, the judge within the budget, no planner past the ladder cap, sensitive fields, a lone
survivor of equal elements, failed retargets counted, playback stats from the session)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.events import EventRecord
from src.browser.playback import BrowserPlaybackAdapter
from src.config import LoopSettings, ReplaySettings, Settings, StorageSettings
from src.contracts import (
    Binding,
    PlaybackRequest,
    RunProfile,
    StepHandoff,
    StepView,
    TaskRequest,
    TaskUsage,
    ToolDef,
)
from src.harness.recorder import Recorder
from src.harness.recording_store import RecordingStore
from src.harness.playback import Retargeter
from src.agent_loop.recordings import RecordedStep, Recording
from tests.test_agent_loop_playback import DONE, ScriptedSource, run as run_playback
from tests.test_approval_judge import PLAN as JUDGE_PLAN, Classifier, Human, Pay, call, judge, run as run_judge
from tests.test_harness_session import install_runner, make_runtime, native_result, read_typed_events

PAGE = """- Page URL: https://shop.example/item/1
- tablist:
  - tab "Specs" [ref=e12]
- textbox "Password" [ref=e20]"""


def ev(kind: str, payload: dict[str, Any]) -> EventRecord:
    return EventRecord(type=kind, source="test", payload=payload, task_id="t", goal_id="t")  # type: ignore[arg-type]


def call_events(recorder: Recorder, tool: str, args: dict[str, Any], content: str = "ok") -> None:
    recorder.emit(ev("tool.started", {"tool_request": {"name": tool, "args": args}, "server": "web", "read_only": False}))
    recorder.emit(ev("tool.finished", {"tool_result": {"name": tool, "status": "success", "content": content}}))


def finish(recorder: Recorder) -> Any:
    return recorder.finish("t", status="completed", stage_reached="agent", catalog={}, source={})


# -- the recorder starts from the carried page --------------------------------------------


def test_a_first_step_on_a_carried_snapshot_is_recorded_with_its_binding(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings())
    recorder = Recorder(store, ReplaySettings(), adapter=BrowserPlaybackAdapter())
    recorder.arm("t", "carried", "Open the specs", snapshots={"web": PAGE})

    call_events(recorder, "click", {"target": "e12"})
    finish(recorder)

    (step,) = store.load("carried").steps
    assert [binding.descriptor["name"] for binding in step.bindings] == ["Specs"]


@pytest.mark.asyncio
async def test_the_session_arms_the_recorder_with_the_carried_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.agent_loop.execution.state import BrowserState

    settings = Settings(replay=ReplaySettings(enabled=True), storage=StorageSettings(root_dir=tmp_path / "home"))
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)
    calls = 0

    async def run(request: TaskRequest) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return native_result(final_answer="seen", browser=BrowserState(snapshots={"": PAGE}))
        events = built[-1].resources.events
        ids = {"task_id": request.task_id, "goal_id": request.task_id}
        events.emit("tool.started", source="test", payload={"tool_request": {"name": "click", "args": {"target": "e12"}}, "server": "", "read_only": False}, **ids)
        events.emit("tool.finished", source="test", payload={"tool_result": {"name": "click", "status": "success", "content": "clicked"}}, **ids)
        return native_result(final_answer="clicked")

    built = install_runner(monkeypatch, run)
    runtime = make_runtime()
    # A plain-text session: use the browser adapter as a browser session would.
    await runtime.start()
    runtime.context.recorder.adapter = BrowserPlaybackAdapter()

    await runtime.run_task("look")
    await runtime.run_task("open specs", profile=RunProfile(record_as="specs"))

    store = RecordingStore.from_settings(settings.replay, settings.storage)
    (step,) = store.load("specs").steps
    assert step.bindings and step.bindings[0].descriptor["name"] == "Specs"
    await runtime.close()


# -- a handoff turn counts toward the agent turn cap ---------------------------------------


@pytest.mark.asyncio
async def test_a_handoff_turn_counts_toward_the_turn_cap() -> None:
    source = ScriptedSource([StepHandoff(reason="Gone.", kind="binding", step=1)])
    view = {"decision": "tool_call", "tool_request": {"name": "view_page", "args": {}, "id": "c1"}}

    result, _, llm, _ = await run_playback(source, [view, DONE], turn_cap=1)

    assert llm.calls == 1
    assert result.status == "blocked" and result.stop_reason == "turn_cap"


# -- the approval classifier never runs past the budget ------------------------------------


@pytest.mark.asyncio
async def test_without_budget_for_the_judge_the_human_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(loop=LoopSettings(max_task_model_calls=2))
    monkeypatch.setattr("src.agent_loop.execution.loop.get_settings", lambda: settings)
    pay, human = Pay(), Human("once")
    classifier = Classifier({"approval": False, "reason": "safe"})

    result, _, _ = await run_judge(
        [JUDGE_PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[pay.tool()],
        approval=judge("classifier", classifier),
        human=human,
    )

    assert classifier.prompts == []  # the judge's call would have been the third
    ((_, reason, verdict),) = human.asked
    assert "approval classifier did not run" in reason and verdict.source == "classifier"
    assert pay.calls == [{"target": "e7"}]
    assert result.stop_reason == "budget_exhausted"


# -- a replay capped below the agent never calls the planner -------------------------------


@pytest.mark.asyncio
async def test_a_capped_replay_without_a_recorded_plan_never_plans_with_the_model() -> None:
    from src.contracts import StepFinal

    source = ScriptedSource([StepFinal("done")], recorded_plan=None)
    profile = RunProfile(playback=PlaybackRequest(recording="r"), max_stage="playback")

    result, _, llm, _ = await run_playback(source, [DONE], profile=profile)

    assert llm.calls == 0 and result.final_answer == "done"


# -- sensitive fields and a lone survivor of equal elements --------------------------------


def test_a_secret_typed_into_a_password_field_is_never_stored(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings())
    recorder = Recorder(store, ReplaySettings(), adapter=BrowserPlaybackAdapter())
    recorder.arm("t", "login", "Log in", snapshots={"web": PAGE})

    call_events(recorder, "type", {"element": "Password", "target": "e20", "text": "hunter2"})
    finish(recorder)

    (step,) = store.load("login").steps
    assert step.redacted is True
    assert "hunter2" not in json.dumps(dict(step.args))
    assert step.args["target"] == "e20"  # the reference stays for diagnostics


def test_one_survivor_of_several_equal_elements_is_not_guessed() -> None:
    adapter = BrowserPlaybackAdapter()
    page = "\n".join(f'- listitem:\n  - button "Delete" [ref=e{i}]' for i in (1, 2, 3))
    (binding,) = adapter.describe({"target": "e3"}, [page])

    result = adapter.rebind({"target": "e3"}, [binding], ['- toolbar:\n  - button "Delete" [ref=e9]'])

    assert result.args is None and "1 of 3" in result.detail


# -- a failed retarget call still counts ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_retarget_that_raises_any_error_is_still_counted() -> None:
    class Broken:
        async def complete(self, messages: Any, **kwargs: Any) -> Any:
            raise ValueError("unparsable reply")

    async def tool_def(name: str) -> ToolDef:
        return ToolDef(name="click")

    events: list[tuple[str, dict]] = []
    step = RecordedStep(index=1, tool="click", args={"target": "e1"}, bindings=(Binding(path=("target",), value="e1"),))
    args, record = await Retargeter(Broken(), BrowserPlaybackAdapter(), ReplaySettings(), tool_def=tool_def).retarget(  # type: ignore[arg-type]
        task="t", step=step, reason="r",
        view=StepView(task="t", turn=1, tool_result={}, snapshots={}, tool_schemas={}),
        page_texts=[PAGE], emit=lambda kind, payload: events.append((kind, dict(payload))),
    )

    assert args is None and record is not None and record.phase == "retarget"
    assert events[-1][1]["error"] == "ValueError"


# -- playback stats come from the session, with the stage the run ended at -----------------


@pytest.mark.asyncio
async def test_the_session_counts_a_replay_with_its_final_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = Settings(replay=ReplaySettings(enabled=True), storage=StorageSettings(root_dir=tmp_path / "home"))
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)
    store = RecordingStore.from_settings(settings.replay, settings.storage)
    store.save(Recording(name="r", task="t", steps=(RecordedStep(index=1, tool="open", args={}),)))

    async def run(request: TaskRequest) -> Any:
        # A replay capped at playback that diverged: blocked, still at stage playback.
        return dataclasses.replace(
            native_result(final_answer="Blocked: playback stopped", status="blocked"),
            usage=TaskUsage(stage_reached="playback"),
            stop_reason="ladder_cap",
        )

    install_runner(monkeypatch, run)
    runtime = make_runtime()

    await runtime.run_task("t", profile=RunProfile(playback=PlaybackRequest(recording="r"), max_stage="playback"))

    stats = store.stats("r")
    assert stats["playbacks"] == 1 and stats["by_stage"] == {"playback": 1}
    assert runtime.context.tasks[-1].status == "blocked"
    assert not [e for e in read_typed_events(runtime.context.session_dir) if e["type"] == "recording.skipped"]
    await runtime.close()
