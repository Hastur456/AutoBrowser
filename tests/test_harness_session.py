from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.agent_loop.execution.state import BrowserState, LoopState
from src.config import HooksSettings, PermissionsSettings, Settings, get_settings
from src.harness.runtime import (
    HARNESS_EVENT_METADATA_CONFIG_KEY,
    HARNESS_STATE_OVERRIDES_CONFIG_KEY,
)
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
from src.harness.hooks import HookConfigError, HookEngine, NullHookEngine, registry_digest
from src.harness.permissions import PermissionEngine
from src.harness.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Keep env-driven config overrides from leaking between tests."""

    yield
    get_settings.cache_clear()

class FakeLLM:
    pass


class FakeTool:
    name = "browser_snapshot"


class FakeToolSource:
    def __init__(self, tools: list[Any] | None = None) -> None:
        self.tools = list(tools if tools is not None else [FakeTool()])

    async def get_tools(self) -> list[Any]:
        return list(self.tools)


class FakeMCPRuntime:
    """Stand-in for :class:`src.harness.mcp_setup.MCPRuntime`."""

    def __init__(self, tools: list[Any] | None = None) -> None:
        self.tool_source = FakeToolSource(tools)
        self.normalizers: list[Any] = []
        self.started = False
        self.closed = False

    async def start(self, *, require_browser: bool = True) -> None:
        _ = require_browser
        self.started = True

    async def close(self) -> None:
        self.closed = True


class FakeHarness:
    def __init__(self, **kwargs: Any) -> None:
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


def native_result(
    final_answer: str = "",
    *,
    status: str = "done",
    **state_updates: Any,
) -> AgentLoopResult:
    """Build a terminal ``AgentLoopResult`` the way the native loop does."""
    state = LoopState(**state_updates)
    return AgentLoopResult(
        status=status,
        final_answer=final_answer,
        session_state=state.to_session_state(),
        state=state,
    )


def install_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: Any,
) -> None:
    """Inject a fake task runner in place of ``native_task_runner``."""
    monkeypatch.setattr(
        "src.harness.session.native_task_runner",
        lambda _resources: runner,
    )


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
        "agent_loop": False,
        "chrome_path": "chrome.exe",
        "user_data_dir": "profile",
        "cdp_port": 9555,
        "cdp_timeout": 1.0,
        "turn_cap": 10,
    }
    values.update(overrides)
    return SessionConfig(**values)


def read_typed_events(session_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


async def noop_wait(_port: int, _timeout: float) -> None:
    return None


def no_mcp_runtime(_port: int) -> FakeMCPRuntime:
    return FakeMCPRuntime([])


def no_start(_chrome_path: str, _user_data_dir: str, _port: int) -> None:
    return None


def llm_factory(**_kwargs: Any) -> FakeLLM:
    return FakeLLM()


def make_runtime(**overrides: Any) -> SessionRuntime:
    return SessionRuntime(
        make_config(**overrides),
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
    )


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
    runtime = FakeMCPRuntime()

    def mcp_runtime_factory(_port: int) -> FakeMCPRuntime:
        return runtime

    context.events.subscribe("session.started", lambda name, _payload: events.append(name))
    context.events.subscribe("task.started", lambda name, _payload: events.append(name))
    context.events.subscribe("task.finished", lambda name, _payload: events.append(name))
    context.events.subscribe("session.closed", lambda name, _payload: events.append(name))

    await context.initialize(
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=mcp_runtime_factory,
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
    assert runtime.started is True
    assert sorted(await context.tool_registry.get_by_name()) == ["browser_snapshot"]

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
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
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
    ) -> AgentLoopResult:
        calls.append((harness, task, config, task_config))
        return native_result(final_answer=f"done: {task}")

    install_runner(monkeypatch, task_runner)
    runtime = make_runtime()

    await runtime.start()
    first_harness = runtime.harness
    await runtime.start()
    result = await runtime.run_task("inspect page")

    assert runtime.harness is first_harness
    assert result.status == "done"
    assert result.final_answer == "done: inspect page"
    assert len(calls) == 1
    assert calls[0][1] == "inspect page"
    assert calls[0][3]["metadata"]["model"] == "test-model"
    assert calls[0][3]["configurable"]["thread_id"] == (
        f"{SESSION_THREAD_PREFIX}{runtime.context.session_id}"
    )
    event_metadata = calls[0][3][HARNESS_EVENT_METADATA_CONFIG_KEY]
    assert event_metadata["session_id"] == runtime.context.session_id
    assert event_metadata["task_id"] == runtime.context.tasks[0].task_id
    assert event_metadata["goal_id"] == runtime.context.tasks[0].task_id
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
    assert typed_events[2]["goal_id"] == runtime.context.tasks[0].task_id


@pytest.mark.asyncio
async def test_session_runtime_watchdog_failure_clears_active_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOBROWSER_LOOP__PROGRESS_TIMEOUT_SECONDS", "0.01")
    get_settings.cache_clear()
    cancelled = False

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: SessionConfig,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return native_result(final_answer="never reached")

    install_runner(monkeypatch, task_runner)
    runtime = make_runtime()

    with pytest.raises(TimeoutError, match="made no progress"):
        await runtime.run_task("inspect page")

    assert cancelled is True
    assert runtime.context.current_task is None
    assert runtime.context.metadata.task_count == 1
    assert len(runtime.context.tasks) == 1
    assert runtime.context.tasks[0].finished_at is not None
    assert isinstance(runtime.context.tasks[0].result, TimeoutError)
    assert runtime.context.session_dir is not None
    typed_events = read_typed_events(runtime.context.session_dir)
    assert [event["type"] for event in typed_events] == [
        "session.started",
        "goal.started",
        "goal.failed",
    ]


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
    ) -> AgentLoopResult:
        calls.append((task, task_config))
        if task == "find products":
            return native_result(
                final_answer="Found Keyboard A.",
                messages=["prior product list"],
                observation="Visible results: first product is Keyboard A.",
                browser=BrowserState(snapshot='- link "Keyboard A" ref=e10'),
            )
        return native_result(final_answer="done")

    install_runner(monkeypatch, task_runner)
    runtime = make_runtime()

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
    assert overrides["snapshot"] == '- link "Keyboard A" ref=e10'
    assert overrides["task_id"] == runtime.context.tasks[1].task_id
    assert overrides["plan"] == []
    assert overrides["decision"] == ""
    assert overrides["final_answer"] == ""
    assert overrides["replan_count"] == 0
    assert overrides["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_session_runtime_remembers_latest_harness_state_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    state_calls: list[tuple[dict[str, Any], Any | None]] = []
    latest_state: dict[str, object] = {
        "messages": ["checkpoint message"],
        "observation": "Checkpoint observation.",
        "snapshot": '- button "Continue" ref=e1',
        "final_answer": "Checkpoint answer.",
    }

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: SessionConfig,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        return native_result(final_answer="runner answer")

    install_runner(monkeypatch, task_runner)

    async def latest_state_loader(
        task_config: dict[str, Any],
        fallback: Any | None,
    ) -> dict[str, object] | None:
        state_calls.append((task_config, fallback))
        return latest_state

    monkeypatch.setattr(
        "src.harness.session.native_latest_state_loader",
        latest_state_loader,
    )
    runtime = make_runtime()

    result = await runtime.run_task("inspect page")

    assert result.final_answer == "runner answer"
    assert dict(runtime.context.state) == latest_state
    assert len(state_calls) == 1
    assert state_calls[0][0]["configurable"]["thread_id"] == (
        f"{SESSION_THREAD_PREFIX}{runtime.context.session_id}"
    )


@pytest.mark.asyncio
async def test_session_runtime_remembers_result_session_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: SessionConfig,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        return native_result(
            final_answer="Result answer.",
            messages=["result message"],
            observation="Result observation.",
        )

    install_runner(monkeypatch, task_runner)
    runtime = make_runtime()

    result = await runtime.run_task("inspect page")

    assert result.final_answer == "Result answer."
    state = dict(runtime.context.state)
    assert state["messages"] == ["result message"]
    assert state["observation"] == "Result observation."


@pytest.mark.asyncio
async def test_session_runtime_emits_goal_failed_and_preserves_exception_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    error = RuntimeError("task failed")

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: SessionConfig,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        raise error

    install_runner(monkeypatch, task_runner)
    runtime = make_runtime()

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.run_task("inspect page")

    assert exc_info.value is error
    assert dict(runtime.context.state) == {}
    assert runtime.context.current_task is None
    assert runtime.context.metadata.task_count == 1
    assert runtime.context.tasks[0].result is error
    assert runtime.context.tasks[0].finished_at is not None
    assert runtime.context.session_dir is not None
    typed_events = read_typed_events(runtime.context.session_dir)
    assert [event["type"] for event in typed_events] == [
        "session.started",
        "goal.started",
        "goal.failed",
    ]
    assert typed_events[1]["goal_id"] == runtime.context.tasks[0].task_id
    assert typed_events[2]["goal_id"] == runtime.context.tasks[0].task_id


# --------------------------------------------------------------------------
# Lifecycle hooks are session-scoped
# --------------------------------------------------------------------------


def use_hooks(monkeypatch: pytest.MonkeyPatch, hooks: HooksSettings) -> None:
    """Point the session at explicit hook settings (never the developer's config.yaml)."""

    settings = Settings(hooks=hooks)
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)


DENY_ALL_HOOKS = HooksSettings(
    enabled=True,
    registry=[{"id": "deny", "event": "pre_tool_use", "handler": "tests.hook_fixtures:deny_all"}],
)


@pytest.mark.asyncio
async def test_session_loads_hooks_once_and_hands_them_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    use_hooks(monkeypatch, DENY_ALL_HOOKS)
    captured: list[Any] = []

    async def task_runner(*_args: Any) -> AgentLoopResult:
        return native_result(final_answer="done")

    def runner_factory(resources: Any) -> Any:
        captured.append(resources)
        return task_runner

    monkeypatch.setattr("src.harness.session.native_task_runner", runner_factory)
    runtime = make_runtime()

    await runtime.run_task("first")
    await runtime.run_task("second")

    hooks = runtime.context.hooks
    assert isinstance(hooks, HookEngine)
    assert hooks.has("pre_tool_use")
    assert [resources.hooks for resources in captured] == [hooks, hooks]
    assert runtime.context.session_dir is not None
    session_payload = json.loads((runtime.context.session_dir / "session.json").read_text())
    assert session_payload["hooks"] == {
        "enabled": True,
        "registry_sha256": registry_digest(DENY_ALL_HOOKS),
    }


@pytest.mark.asyncio
async def test_disabled_hooks_give_the_engine_a_null_hook_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    use_hooks(monkeypatch, HooksSettings(enabled=False, registry=DENY_ALL_HOOKS.registry))
    captured: list[Any] = []

    async def task_runner(*_args: Any) -> AgentLoopResult:
        return native_result(final_answer="done")

    def runner_factory(resources: Any) -> Any:
        captured.append(resources)
        return task_runner

    monkeypatch.setattr("src.harness.session.native_task_runner", runner_factory)
    runtime = make_runtime()

    await runtime.run_task("inspect page")

    assert isinstance(runtime.context.hooks, NullHookEngine)
    assert isinstance(captured[0].hooks, NullHookEngine)
    assert runtime.context.session_dir is not None
    session_payload = json.loads((runtime.context.session_dir / "session.json").read_text())
    assert session_payload["hooks"]["enabled"] is False


@pytest.mark.asyncio
async def test_a_broken_hook_registry_fails_session_start_before_chrome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    use_hooks(
        monkeypatch,
        HooksSettings(
            enabled=True,
            registry=[{"id": "x", "event": "stop", "handler": "tests.no_such_module:f"}],
        ),
    )
    launched: list[int] = []

    def start_chrome(_path: str, _profile: str, port: int) -> None:
        launched.append(port)

    runtime = SessionRuntime(
        make_config(no_mcp=False),
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
    )

    with pytest.raises(HookConfigError, match="cannot import"):
        await runtime.start()

    assert launched == []
    assert runtime.context.initialized is False


class PlanAndDoneModel:
    """Chat model whose single response works as a plan and as a ``done`` answer."""

    RESPONSE = json.dumps(
        {
            "steps": [{"id": 1, "description": "Answer", "status": "pending"}],
            "decision": "done",
            "final_answer": "Done.",
        }
    )

    async def complete(self, messages: Any, **_kwargs: Any) -> Any:
        from src.llm import ModelResponse

        return ModelResponse(content=self.RESPONSE, finish_reason="stop")


@pytest.mark.asyncio
async def test_stop_blocks_start_from_zero_in_every_task_of_a_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.hook_fixtures import CALLS

    monkeypatch.chdir(tmp_path)
    use_hooks(
        monkeypatch,
        HooksSettings(
            enabled=True,
            max_stop_blocks=1,
            registry=[{"id": "deny", "event": "stop", "handler": "tests.hook_fixtures:deny_all"}],
        ),
    )
    CALLS.clear()
    runtime = SessionRuntime(
        make_config(),
        llm_factory=lambda **_kwargs: PlanAndDoneModel(),
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
    )

    first = await runtime.run_task("first task")
    second = await runtime.run_task("second task")

    # Each task: the first done is rejected (stop_blocks 0 -> 1), the second is accepted
    # because the budget of 1 is spent. A carried-over stop_blocks would skip the hook in
    # the second task entirely.
    assert first.status == second.status == "done"
    assert first.state.stop_blocks == second.state.stop_blocks == 1
    assert [(event.task, event.stop_hook_active) for _, event in CALLS] == [
        ("first task", False),
        ("second task", False),
    ]
    assert "stop_blocks" not in runtime.context.state
    CALLS.clear()


# --------------------------------------------------------------------------
# Permissions are session-scoped
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_builds_one_permission_engine_for_every_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        permissions=PermissionsSettings(
            mode="dont_ask",
            rules=[{"id": "no-upload", "decision": "deny", "tool": "browser_file_upload"}],
        )
    )
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)
    captured: list[Any] = []

    async def task_runner(*_args: Any) -> AgentLoopResult:
        return native_result(final_answer="done")

    def runner_factory(resources: Any) -> Any:
        captured.append(resources)
        return task_runner

    monkeypatch.setattr("src.harness.session.native_task_runner", runner_factory)
    runtime = make_runtime()

    await runtime.run_task("first")
    runtime.context.permissions.grant(("playwright", "browser_click", "ozon.ru"))
    await runtime.run_task("second")

    permissions = runtime.context.permissions
    assert isinstance(permissions, PermissionEngine)
    assert permissions.mode == "dont_ask"
    assert [resources.permissions for resources in captured] == [permissions, permissions]
    # A grant from one task is still there for the next one.
    assert captured[1].permissions.grants == {("playwright", "browser_click", "ozon.ru")}
    assert runtime.context.session_dir is not None
    session_payload = json.loads((runtime.context.session_dir / "session.json").read_text())
    assert session_payload["permissions"] == {"mode": "dont_ask"}


@pytest.mark.asyncio
async def test_a_rule_clashing_with_a_builtin_fails_session_start_before_chrome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        permissions=PermissionsSettings(
            rules=[{"id": "sensitive-tool-name", "decision": "allow"}],
        )
    )
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)
    launched: list[int] = []

    def start_chrome(_path: str, _profile: str, port: int) -> None:
        launched.append(port)

    runtime = SessionRuntime(
        make_config(no_mcp=False),
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
    )

    with pytest.raises(ValueError, match="duplicate permission rule id"):
        await runtime.start()

    assert launched == []
    assert runtime.context.initialized is False
