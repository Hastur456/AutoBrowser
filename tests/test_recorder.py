"""``Recorder``: completed runs become recordings, from raw session events only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.evals import FakeChatModel
from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink, CompositeEventSink
from src.agent_loop.execution.loop import AgentLoopEngine
from src.agent_loop.execution.resources import EngineResources
from src.browser.playback import BrowserPlaybackAdapter
from src.config import ReplaySettings
from src.contracts import Tool
from src.harness.hooks import NullHookEngine
from src.harness.recorder import Recorder
from src.harness.recording_store import RecordingStore
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry

PAGE = """- Page URL: https://shop.example/item/1
- tablist [ref=e10]:
  - tab "Description" [ref=e11]
  - tab "Specs" [ref=e12]
- button "Buy now" [ref=e20]"""
AFTER_CLICK = """- Page URL: https://shop.example/item/1
- tabpanel "Specs" [ref=e30]:
  - cell "Weight 1.2 kg" [ref=e31]"""
TASK = "task-1"


def ev(kind: str, payload: dict[str, Any], *, task_id: str = TASK) -> EventRecord:
    return EventRecord(type=kind, source="test", payload=payload, task_id=task_id, goal_id=task_id)  # type: ignore[arg-type]


def call(recorder: Recorder, tool: str, args: dict[str, Any], *, content: str = "", status: str = "success",
         server: str = "web", read_only: bool = False, task_id: str = TASK) -> None:
    recorder.emit(ev("tool.started", {"tool_request": {"name": tool, "args": args}, "server": server,
                                      "read_only": read_only}, task_id=task_id))
    recorder.emit(ev("tool.finished", {"tool_result": {"name": tool, "status": status, "content": content}},
                     task_id=task_id))


@pytest.fixture
def store(tmp_path: Path) -> RecordingStore:
    return RecordingStore(tmp_path, ReplaySettings())


def make_recorder(store: RecordingStore, **settings: Any) -> Recorder:
    return Recorder(
        store,
        ReplaySettings(**settings),
        adapter=BrowserPlaybackAdapter(ReplaySettings()),
        service_servers=frozenset({"memory"}),
    )


def finish(recorder: Recorder, *, status: str = "completed", stage: str = "agent") -> Any:
    return recorder.finish(
        TASK,
        status=status,
        stage_reached=stage,
        catalog={"open": {"server": "web", "read_only": False, "schema_digest": "sha256:a"},
                 "snapshot": {"server": "web", "read_only": True, "schema_digest": "sha256:b"},
                 "click": {"server": "web", "read_only": False, "schema_digest": "sha256:c"},
                 "unused": {"server": "web", "read_only": True, "schema_digest": "sha256:d"}},
        source={"task_id": TASK, "model": "m"},
    )


def test_a_completed_run_is_recorded_with_bindings_and_checkpoints(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "price-watch", "Read the specs")
    recorder.emit(ev("plan.updated", {"steps": [{"id": 1, "description": "Open", "status": "in_progress"}],
                                      "current_step": 0}))
    call(recorder, "open", {"url": "https://shop.example/item/1"}, content="- Page URL: https://shop.example/item/1")
    call(recorder, "snapshot", {}, content=PAGE, read_only=True)
    call(recorder, "click", {"element": "Specs tab", "target": "e12"}, content=AFTER_CLICK)

    outcome = finish(recorder)

    assert outcome.saved and outcome.steps == 3 and not outcome.replaced
    recording = store.load("price-watch")
    assert [step.tool for step in recording.steps] == ["open", "snapshot", "click"]
    assert recording.plan == ({"id": 1, "description": "Open", "status": "pending"},)
    click = recording.steps[2]
    (binding,) = click.bindings
    assert (binding.path, binding.value, binding.descriptor["name"]) == (("target",), "e12", "Specs")
    assert click.plan_step == 0
    assert click.checkpoint["url"] == "shop.example/item/1"
    # Only the tools the run used, with their catalog entries.
    assert set(recording.catalog) == {"open", "snapshot", "click"}
    assert recording.source["stage_reached"] == "agent"
    assert recording.answer == "agent"


def test_failed_service_and_unarmed_calls_are_not_recorded(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "run", "t")
    call(recorder, "open", {"url": "https://x.example"}, status="error")
    call(recorder, "memory_view", {"path": "sites/x.md"}, server="memory", read_only=True)
    call(recorder, "open", {"url": "https://y.example"}, task_id="another-task")
    call(recorder, "open", {"url": "https://z.example"})

    finish(recorder)

    (step,) = store.load("run").steps
    assert step.args == {"url": "https://z.example"}


def test_a_blocked_call_never_started_and_is_not_recorded(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "run", "t")
    # A permission deny emits no tool.started/tool.finished pair.
    recorder.emit(ev("permission.decided", {"tool": "click", "decision": "deny"}))
    call(recorder, "open", {"url": "https://z.example"})

    finish(recorder)

    assert [step.tool for step in store.load("run").steps] == ["open"]


def test_a_secret_argument_is_redacted_and_the_step_marked(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "login", "t")
    call(recorder, "fill", {"target": "e5", "password": "hunter2"})

    finish(recorder)

    (step,) = store.load("login").steps
    assert step.redacted is True
    assert "hunter2" not in json.dumps(step.args)


def test_the_page_text_before_the_step_is_used_and_a_state_change_drops_it(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "run", "t")
    call(recorder, "snapshot", {}, content=PAGE, read_only=True)
    call(recorder, "click", {"target": "e12"}, content="clicked")  # drops the kept snapshot
    # e20 is only in the dropped snapshot; the latest output does not list it.
    call(recorder, "click", {"target": "e20"}, content="clicked again")

    finish(recorder)

    steps = store.load("run").steps
    assert len(steps[1].bindings) == 1
    assert steps[2].bindings == ()


@pytest.mark.parametrize(
    "status, stage, steps, reason",
    [
        ("blocked", "agent", 1, "ended blocked"),
        ("completed", "agent", 0, "no successful tool call"),
        ("completed", "agent", 3, "exceed replay.max_steps"),
    ],
)
def test_runs_that_should_not_be_recorded_are_skipped(
    store: RecordingStore, status: str, stage: str, steps: int, reason: str
) -> None:
    recorder = make_recorder(store, max_steps=2)
    recorder.arm(TASK, "run", "t")
    for index in range(steps):
        call(recorder, "open", {"url": f"https://x.example/{index}"})

    outcome = finish(recorder, status=status, stage=stage)

    assert not outcome.saved and reason in outcome.reason
    assert not store.exists("run")


def test_a_replay_that_stayed_in_playback_keeps_the_recording(store: RecordingStore) -> None:
    recorder = make_recorder(store)
    recorder.arm(TASK, "run", "t")
    call(recorder, "open", {"url": "https://x.example"})
    finish(recorder)

    recorder.arm(TASK, "run", "t")
    call(recorder, "open", {"url": "https://other.example"})
    skipped = finish(recorder, stage="playback")
    recorder.arm(TASK, "run", "t")
    call(recorder, "open", {"url": "https://third.example"})
    replaced = finish(recorder, stage="retarget")

    assert not skipped.saved and "never left playback" in skipped.reason
    assert replaced.saved and replaced.replaced
    assert store.load("run").steps[0].args == {"url": "https://third.example"}


def test_a_failing_adapter_never_breaks_the_task(store: RecordingStore) -> None:
    class Broken(BrowserPlaybackAdapter):
        def describe(self, args, page_texts):  # noqa: ANN001, ANN201
            raise RuntimeError("bad snapshot")

    recorder = Recorder(store, ReplaySettings(), adapter=Broken())
    recorder.arm(TASK, "run", "t")
    call(recorder, "open", {"url": "https://x.example"})  # must not raise

    outcome = finish(recorder)

    assert not outcome.saved and "capture failed: RuntimeError" in outcome.reason


def test_an_unarmed_task_finishes_with_nothing(store: RecordingStore) -> None:
    assert make_recorder(store).finish(TASK, status="completed", stage_reached="agent", catalog={}, source={}) is None


def test_an_invalid_name_is_refused_when_arming(store: RecordingStore) -> None:
    with pytest.raises(ValueError):
        make_recorder(store).arm(TASK, "../x", "t")


@pytest.mark.asyncio
async def test_the_recorder_mirror_matches_the_engine_snapshots(store: RecordingStore) -> None:
    """Contract: the recorder's page view equals ``LoopState.browser.snapshots``."""

    pages = iter([PAGE, AFTER_CLICK])

    async def snapshot() -> str:
        return next(pages)

    async def click(target: str) -> str:
        return "clicked"

    async def lookup(q: str) -> str:
        return f"docs for {q}"

    tools = [
        Tool(name="snapshot", func=snapshot, annotations={"readOnlyHint": True}),
        Tool(name="click", func=click, input_schema={"type": "object", "properties": {"target": {"type": "string"}}}),
        Tool(name="lookup", func=lookup, annotations={"readOnlyHint": True},
             input_schema={"type": "object", "properties": {"q": {"type": "string"}}}),
    ]
    recorder = make_recorder(store)
    sink = InMemoryEventSink()
    emitter = EventEmitter(CompositeEventSink([sink, recorder]), session_id="s")
    llm = FakeChatModel(
        [
            json.dumps({"steps": [{"id": 1, "description": "Look", "status": "pending"}]}),
            json.dumps({"decision": "tool_call", "tool_request": {"name": "snapshot", "args": {}}}),
            json.dumps({"decision": "tool_call", "tool_request": {"name": "lookup", "args": {"q": "a"}}}),
            json.dumps({"decision": "tool_call", "tool_request": {"name": "click", "args": {"target": "e12"}}}),
            json.dumps({"decision": "tool_call", "tool_request": {"name": "snapshot", "args": {}}}),
            json.dumps({"decision": "done", "final_answer": "Weight 1.2 kg"}),
        ]
    )
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=tools), event_emitter=emitter)
    resources = EngineResources.from_harness(harness, llm=llm, events=emitter, hooks=NullHookEngine())
    recorder.arm(TASK, "mirror", "Look")

    result = await AgentLoopEngine(resources).run("Look", task_id=TASK, session_id="s")

    capture = recorder._captures[TASK]  # noqa: SLF001 - the contract under test
    assert capture.snapshots == result.state.browser.snapshots
    assert capture.latest == result.state.tool_result.get("content")
    assert finish(recorder).steps == 4


def test_the_tool_catalog_reports_server_read_only_and_a_schema_digest() -> None:
    from src.agent_loop.execution.tools import tool_catalog
    from src.agent_loop.recordings import schema_digest

    async def noop() -> str:
        return ""

    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    reader = Tool(name="read", func=noop, input_schema=schema, annotations={"readOnlyHint": True})
    writer = Tool(name="write", func=noop)

    catalog = tool_catalog([reader, writer])

    assert catalog["read"] == {"server": "", "read_only": True, "schema_digest": schema_digest(schema)}
    assert catalog["write"]["read_only"] is False
