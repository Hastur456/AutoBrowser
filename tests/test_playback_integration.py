"""Record a run over a real MCP server, then replay it where the refs, the layout or the
page path changed (``tests/mcp_fixtures/fake_page_server.py``)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.evals import FakeChatModel
from src.agent_loop.events import CompositeEventSink, EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.loop import AgentLoopEngine, AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.tools import tool_catalog
from src.browser.playback import BrowserPlaybackAdapter
from src.config import ReplaySettings
from src.contracts import PlaybackRequest, RunProfile
from src.harness.mcp_tools import MCPToolSource
from src.harness.playback import PlaybackFactory
from src.harness.recorder import Recorder
from src.harness.recording_store import RecordingStore
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelResponse
from src.mcp import MCPManager

PAGE_SERVER = str(Path(__file__).parent / "mcp_fixtures" / "fake_page_server.py")
TASK = "Read the kettle specs."
PLAN = {"steps": [{"id": 1, "description": "Open the kettle", "status": "pending"},
                  {"id": 2, "description": "Read the specs", "status": "pending"}]}
DONE = {"decision": "done", "final_answer": "The kettle costs 42."}


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"decision": "tool_call",
            "tool_request": {"name": f"page__{name}", "args": args, "id": f"call-{name}-{len(args)}"}}


class CountingModel(FakeChatModel):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__([json.dumps(response) for response in responses])
        self.calls = 0

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        content = self._responses[min(self.calls, len(self._responses)) - 1]
        return ModelResponse(content=content, finish_reason="stop")


def server(*args: str) -> dict[str, Any]:
    return {"transport": "stdio", "command": sys.executable, "args": [PAGE_SERVER, *args],
            "reconnect": {"base_delay_s": 0.05}}


async def run(
    config: dict[str, Any],
    responses: list[dict[str, Any]],
    *,
    store: RecordingStore,
    profile: RunProfile | None = None,
    record: bool = False,
) -> tuple[AgentLoopResult, list[EventRecord], CountingModel, list[str]]:
    settings = store.settings
    adapter = BrowserPlaybackAdapter(settings)
    async with MCPManager({"page": config}) as manager:
        registry = ToolRegistry(providers=[MCPToolSource(manager)])
        recorder = Recorder(store, settings, adapter=adapter)
        sink = InMemoryEventSink()
        emitter = EventEmitter(CompositeEventSink([sink, recorder]), session_id="s")
        llm = CountingModel(responses)
        harness = BrowserHarness(llm=llm, tool_registry=registry, event_emitter=emitter)
        resources = EngineResources.from_harness(
            harness, llm=llm, events=emitter, steps=PlaybackFactory(store, adapter, settings)
        )
        if record:
            recorder.arm("task-1", "kettle-specs", TASK)
        result = await AgentLoopEngine(resources).run(TASK, task_id="task-1", session_id="s", profile=profile)
        notes: list[str] = []
        if record:
            outcome = recorder.finish(
                "task-1",
                status="completed" if result.status == "done" else result.status,
                stage_reached=result.state.usage.stage_reached,
                catalog=tool_catalog(list(await registry.get_all())),
                source={"task_id": "task-1"},
            )
            notes.append(outcome.reason if outcome and not outcome.saved else "saved")
    return result, list(sink.records), llm, notes


def clicks(records: list[EventRecord]) -> list[str]:
    return [
        str(record.payload["tool_request"]["args"].get("target"))
        for record in records
        if record.type == "tool.started" and record.payload["tool_request"]["name"] == "page__click"
    ]


def replay(answer: str = "output") -> RunProfile:
    return RunProfile(
        playback=PlaybackRequest(recording="kettle-specs", answer=answer, select=r"(?s)tabpanel.*")  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_record_then_replay_where_refs_layout_and_path_change(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings())

    async def scenario() -> None:
        # 1. The agent does the task once; the run is recorded (refs e20..e22).
        recorded, _, llm, notes = await run(
            server("--ref-base", "10"),
            [PLAN, call("navigate", url="https://shop.example/item/1"), call("snapshot"),
             call("click", element="Specs tab", target="e21"), DONE],
            store=store,
            record=True,
        )
        assert recorded.status == "done" and notes == ["saved"] and llm.calls == 5
        recording = store.load("kettle-specs")
        assert [step.tool for step in recording.steps] == ["page__navigate", "page__snapshot", "page__click"]
        assert recording.steps[2].bindings[0].descriptor["name"] == "Specs"

        # 2. Another browser process: every ref differs, the recording still replays with
        #    no model call at all.
        replayed, records, llm, _ = await run(server("--ref-base", "100"), [DONE], store=store, profile=replay())
        assert llm.calls == 0
        assert replayed.status == "done" and replayed.stop_reason == "playback_completed"
        assert "Price 42" in replayed.final_answer and "[ref=" not in replayed.final_answer
        assert clicks(records) == ["e111"]

        # 3. The Specs tab was renamed: the binding diverges before the click, the agent
        #    takes over with the replayed history.
        changed, records, llm, _ = await run(
            server("--ref-base", "100", "--layout", "changed"), [DONE], store=store, profile=replay()
        )
        assert llm.calls == 1 and changed.final_answer == "The kettle costs 42."
        assert clicks(records) == []
        diverged = [record.payload for record in records if record.type == "playback.diverged"]
        assert [(event["kind"], event["step"]) for event in diverged] == [("binding", 3)]

        # 4. The product moved to another path: the checkpoint after navigate fails and the
        #    agent takes over instead of clicking on an unexpected page.
        moved, records, llm, _ = await run(
            server("--ref-base", "100", "--layout", "moved"), [DONE], store=store, profile=replay()
        )
        assert llm.calls == 1
        assert [record.payload["kind"] for record in records if record.type == "playback.diverged"] == ["checkpoint"]
        assert clicks(records) == []

        stats = store.stats("kettle-specs")
        assert stats["playbacks"] == 3
        assert stats["diverged"] == {"binding": 1, "checkpoint": 1}

    await asyncio.wait_for(scenario(), timeout=120)
