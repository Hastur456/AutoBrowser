from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.harness.session import (
    ArtifactRegistry,
    SESSION_THREAD_PREFIX,
    SessionConfig,
    SessionContext,
    SessionEventBus,
    SessionRuntime,
    SessionState,
    WorkspaceContext,
)
from src.harness.runtime import HARNESS_STATE_OVERRIDES_CONFIG_KEY
from src.harness.tools import ToolRegistry


class FakeLLM:
    pass


class FakeTool:
    name = "browser_snapshot"


class FakeBrowserProvider:
    def __init__(self, tools: list[Any] | None = None) -> None:
        self.tools = list(tools or [FakeTool()])

    async def get_tools(self) -> list[Any]:
        return list(self.tools)

    def normalize_request(
        self,
        request: dict[str, Any],
        _state: dict[str, Any],
    ) -> dict[str, Any]:
        return request

    def normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return result


class FakeHarness:
    def __init__(self, graph_builder: Any, **kwargs: Any) -> None:
        self.graph_builder = graph_builder
        self.kwargs = kwargs


class FakeChromeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.waited = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> None:
        _ = timeout
        self.waited = True


def make_config(**overrides: Any) -> SessionConfig:
    values = {
        "model": "test-model",
        "temperature": 0.1,
        "no_mcp": True,
        "show_state": False,
        "hide_snapshot": False,
        "show_tools": False,
        "as_json": False,
        "compress_tools": False,
        "chrome_path": "chrome.exe",
        "user_data_dir": "profile",
        "cdp_port": 9555,
        "cdp_timeout": 1.0,
        "recursion_limit": 10,
        "tracing_enabled": False,
    }
    values.update(overrides)
    return SessionConfig(**values)


async def noop_wait(_port: int, _timeout: float) -> None:
    return None


async def no_browser_provider(_port: int) -> FakeBrowserProvider:
    return FakeBrowserProvider([])


async def noop_close() -> None:
    return None


def no_start(_chrome_path: str, _user_data_dir: str, _port: int) -> None:
    return None


def llm_factory(**_kwargs: Any) -> FakeLLM:
    return FakeLLM()


def test_session_state_wraps_mapping_operations() -> None:
    state = SessionState()

    state.set("retry_count", 1)
    state["current_url"] = "https://example.test"
    removed = state.pop("retry_count")

    assert removed == 1
    assert state.get("current_url") == "https://example.test"
    assert list(state.items()) == [("current_url", "https://example.test")]
    state.clear()
    assert len(state) == 0


def test_workspace_context_creates_standard_directories(tmp_path: Path) -> None:
    workspace = WorkspaceContext(tmp_path / "workspace")

    workspace.initialize()

    assert workspace.root.is_dir()
    assert workspace.downloads.is_dir()
    assert workspace.screenshots.is_dir()
    assert workspace.temp.is_dir()
    assert workspace.artifacts.is_dir()


def test_artifact_registry_tracks_latest_artifact_by_kind(tmp_path: Path) -> None:
    registry = ArtifactRegistry()
    first = registry.register("first.png", tmp_path / "first.png", kind="screenshot")
    second = registry.register("report.csv", tmp_path / "report.csv", kind="table")

    assert registry.latest() == second
    assert registry.latest("screenshot") == first
    assert registry.latest("missing") is None
    assert registry.all() == [first, second]


def test_session_event_bus_emits_to_subscribers_in_registration_order() -> None:
    bus = SessionEventBus()
    events: list[tuple[str, object | None, str]] = []

    bus.emit("task.started", {"task": "ignored"})
    bus.subscribe("task.started", lambda name, payload: events.append((name, payload, "a")))
    bus.subscribe("task.started", lambda name, payload: events.append((name, payload, "b")))

    payload = {"task": "inspect"}
    bus.emit("task.started", payload)

    assert events == [
        ("task.started", payload, "a"),
        ("task.started", payload, "b"),
    ]


@pytest.mark.asyncio
async def test_session_context_lifecycle_initializes_tracks_tasks_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = SessionContext(make_config(no_mcp=False))
    events: list[str] = []
    provider = FakeBrowserProvider()

    async def load_browser_provider(_port: int) -> FakeBrowserProvider:
        return provider

    context.events.subscribe("session.started", lambda name, _payload: events.append(name))
    context.events.subscribe("task.started", lambda name, _payload: events.append(name))
    context.events.subscribe("task.finished", lambda name, _payload: events.append(name))
    context.events.subscribe("session.closed", lambda name, _payload: events.append(name))

    await context.initialize(
        graph_builder=lambda **_kwargs: object(),
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        load_browser_provider=load_browser_provider,
        output_fn=lambda *_args, **_kwargs: None,
        print_tools=None,
        harness_factory=FakeHarness,
    )

    assert context.initialized is True
    assert isinstance(context.harness, FakeHarness)
    assert context.workspace is not None
    assert context.workspace.root == tmp_path / ".autobrowser" / "sessions" / context.session_id / "workspace"
    assert context.workspace.artifacts.is_dir()
    assert context.metadata.started_at is not None
    assert context.tool_registry is not None
    assert context.tool_registry.get_browser_providers() == [provider]

    record = context.reset_task("inspect page")
    assert context.current_task == "inspect page"
    assert context.tasks == [record]

    result = {"final_answer": "done"}
    context.finish_task(record, result)
    assert record.result == result
    assert record.finished_at is not None
    assert context.current_task is None
    assert context.metadata.task_count == 1
    assert context.session_dir is not None
    session_payload = json.loads((context.session_dir / "session.json").read_text())
    tasks_payload = json.loads((context.session_dir / "tasks.json").read_text())
    assert session_payload["session_id"] == context.session_id
    assert session_payload["metadata"]["task_count"] == 1
    assert tasks_payload[0]["task"] == "inspect page"
    assert tasks_payload[0]["task_id"] == record.task_id
    assert tasks_payload[0]["result"] == result

    await context.close()
    assert context.initialized is False
    assert context.harness is None
    assert context.llm is None
    closed_payload = json.loads((context.session_dir / "session.json").read_text())
    assert closed_payload["initialized"] is False
    assert context.session_dir is not None
    typed_events = [
        json.loads(line)
        for line in (context.session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in typed_events] == [
        "session.started",
        "session.closed",
    ]
    assert events == [
        "session.started",
        "task.started",
        "task.finished",
        "session.closed",
    ]


@pytest.mark.asyncio
async def test_session_context_closes_owned_chrome_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = SessionContext(make_config(no_mcp=False))
    process = FakeChromeProcess()

    def start_chrome(
        _chrome_path: str,
        _user_data_dir: str,
        _port: int,
    ) -> FakeChromeProcess:
        return process

    await context.initialize(
        graph_builder=lambda **_kwargs: object(),
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=noop_wait,
        load_browser_provider=no_browser_provider,
        output_fn=lambda *_args, **_kwargs: None,
        print_tools=None,
        harness_factory=FakeHarness,
    )

    await context.close()

    assert process.terminated is True
    assert process.waited is True
    assert context.chrome_process is None


@pytest.mark.asyncio
async def test_session_runtime_reuses_context_and_records_task_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[Any] = []

    async def task_runner(
        harness: Any,
        task: str,
        config: SessionConfig,
        task_config: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append((harness, task, config, task_config))
        return {"final_answer": f"done: {task}"}

    runtime = SessionRuntime(
        make_config(),
        graph_builder=lambda **_kwargs: object(),
        llm_factory=llm_factory,
        task_runner=task_runner,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        load_browser_provider=no_browser_provider,
        close_mcp_session=noop_close,
        harness_factory=FakeHarness,
    )

    await runtime.start()
    first_harness = runtime.harness
    await runtime.start()
    result = await runtime.run_task("inspect page")

    assert runtime.harness is first_harness
    assert result == {"final_answer": "done: inspect page"}
    assert len(calls) == 1
    assert calls[0][1] == "inspect page"
    assert calls[0][3]["metadata"]["model"] == "test-model"
    assert calls[0][3]["configurable"]["thread_id"] == (
        f"{SESSION_THREAD_PREFIX}{runtime.context.session_id}"
    )
    assert runtime.context.current_task is None
    assert runtime.context.metadata.task_count == 1
    assert runtime.context.tasks[0].task == "inspect page"
    assert runtime.context.tasks[0].result == result
    assert isinstance(runtime.context.tool_registry, ToolRegistry)
    assert runtime.context.session_dir is not None
    typed_events = [
        json.loads(line)
        for line in (runtime.context.session_dir / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    agent_trace = [
        json.loads(line)
        for line in (runtime.context.session_dir / "agent_trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["type"] for event in typed_events] == [
        "session.started",
        "goal.started",
        "goal.completed",
    ]
    assert [event["type"] for event in agent_trace] == [
        "goal.started",
        "goal.completed",
    ]
    assert typed_events[1]["goal_id"] == runtime.context.tasks[0].task_id


@pytest.mark.asyncio
async def test_session_runtime_carries_browser_state_between_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def task_runner(
        _harness: Any,
        task: str,
        _config: SessionConfig,
        task_config: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append((task, task_config))
        if task == "find products":
            return {
                "messages": ["prior product list"],
                "observation": "Visible results: first product is Keyboard A.",
                "snapshot": "- link \"Keyboard A\" ref=e10",
                "decision": "done",
                "final_answer": "Found Keyboard A.",
                "plan": [{"id": 1, "description": "old", "status": "completed"}],
                "replan_count": 2,
                "consecutive_failures": 1,
            }
        return {"final_answer": "done"}

    runtime = SessionRuntime(
        make_config(),
        graph_builder=lambda **_kwargs: object(),
        llm_factory=llm_factory,
        task_runner=task_runner,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        load_browser_provider=no_browser_provider,
        close_mcp_session=noop_close,
        harness_factory=FakeHarness,
    )

    await runtime.run_task("find products")
    await runtime.run_task("open the first one")

    first_config = calls[0][1]
    second_config = calls[1][1]
    assert first_config["configurable"]["thread_id"] == second_config["configurable"]["thread_id"]
    assert first_config["configurable"]["thread_id"] == (
        f"{SESSION_THREAD_PREFIX}{runtime.context.session_id}"
    )

    overrides = second_config[HARNESS_STATE_OVERRIDES_CONFIG_KEY]
    assert overrides["messages"] == ["prior product list"]
    assert overrides["observation"] == "Visible results: first product is Keyboard A."
    assert overrides["snapshot"] == "- link \"Keyboard A\" ref=e10"
    assert overrides["task_id"] == runtime.context.tasks[1].task_id
    assert overrides["plan"] == []
    assert overrides["decision"] == ""
    assert overrides["final_answer"] == ""
    assert overrides["replan_count"] == 0
    assert overrides["consecutive_failures"] == 0
