from __future__ import annotations

import argparse
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.agent_loop.execution.state import LoopState
from src.browser import PlaywrightMCPBrowserProvider
from src.cli import bootstrap
from src.cli.output import format_state, print_tools
from src.cli.parser import build_parser
from src.cli.tasks import resolve_task
from src.harness import chrome, langsmith
from src.harness.chrome import start_chrome_cdp
from src.mcp import mcp_setup, playwright_runtime


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeBrowserProvider:
    def __init__(self, tools: list[FakeTool]) -> None:
        self.tools = list(tools)

    async def get_tools(self) -> list[FakeTool]:
        return list(self.tools)

    def normalize_request(
        self,
        request: dict[str, Any],
        _state: dict[str, Any],
    ) -> dict[str, Any]:
        return request

    def normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return result


def make_args(**overrides: Any) -> argparse.Namespace:
    values = {
        "task": ["inspect", "page"],
        "task_text": None,
        "loop": False,
        "model": "fake-model",
        "temperature": 0,
        "show_state": False,
        "hide_snapshot": False,
        "show_tools": False,
        "json": False,
        "no_mcp": True,
        "compress_tools": False,
        "agent_loop": False,
        "chrome_path": "chrome.exe",
        "user_data_dir": "profile",
        "cdp_port": 9222,
        "cdp_timeout": 1,
        "recursion_limit": 3,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def native_result(final_answer: str = "", *, status: str = "done") -> AgentLoopResult:
    """Build a terminal ``AgentLoopResult`` the way the native loop does."""
    return AgentLoopResult(
        status=status,
        final_answer=final_answer,
        session_state={},
        state=LoopState(),
        turns=0,
    )


def install_runner(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    """Inject a fake task runner in place of ``native_task_runner``."""
    monkeypatch.setattr(
        "src.harness.session.native_task_runner",
        lambda _resources: runner,
    )


def build_session_with(
    monkeypatch: pytest.MonkeyPatch,
    **overrides: Any,
) -> None:
    """Wrap ``build_session`` to inject overrides (its defaults are bound at def time)."""
    real_build_session = bootstrap.build_session

    def wrapped(args: argparse.Namespace, **kwargs: Any) -> Any:
        return real_build_session(args, **overrides, **kwargs)

    monkeypatch.setattr(bootstrap, "build_session", wrapped)


def exit_session_after_initial_task(monkeypatch: pytest.MonkeyPatch) -> None:
    build_session_with(monkeypatch, input_fn=lambda _prompt="": "quit")


def llm_factory(**_kwargs: Any) -> object:
    return object()


def test_parser_accepts_cli_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "open",
            "example.com",
            "--task",
            "explicit task",
            "--loop",
            "--show-state",
            "--hide-snapshot",
            "--show-tools",
            "--json",
            "--no-mcp",
            "--compress-tools",
            "--chrome-path",
            "chrome.exe",
            "--user-data-dir",
            "profile",
            "--cdp-port",
            "9333",
            "--cdp-timeout",
            "5",
            "--recursion-limit",
            "7",
        ]
    )

    assert args.task == ["open", "example.com"]
    assert args.task_text == "explicit task"
    assert args.loop is True
    assert args.show_state is True
    assert args.hide_snapshot is True
    assert args.show_tools is True
    assert args.json is True
    assert args.no_mcp is True
    assert args.compress_tools is True
    assert args.agent_loop is False
    assert args.chrome_path == "chrome.exe"
    assert args.user_data_dir == "profile"
    assert args.cdp_port == 9333
    assert args.cdp_timeout == 5
    assert args.recursion_limit == 7


def test_parser_accepts_agent_loop_flag() -> None:
    parser = build_parser()

    args = parser.parse_args(["--agent-loop"])

    assert args.agent_loop is True


def test_format_state_json() -> None:
    assert format_state({"text": "привет"}, as_json=True) == '{\n  "text": "привет"\n}'


def test_print_tools(capsys) -> None:
    print_tools([FakeTool("browser_navigate"), FakeTool("browser_snapshot")])

    output = capsys.readouterr().out
    assert "MCP tools:" in output
    assert "- browser_navigate" in output
    assert "- browser_snapshot" in output


def test_configure_langsmith_tracing_enables_legacy_vars(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "browser-runs")
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    assert langsmith.configure_langsmith_tracing() is True
    assert langsmith.os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert langsmith.os.environ["LANGCHAIN_PROJECT"] == "browser-runs"


def test_configure_langsmith_tracing_sets_default_project(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    assert langsmith.configure_langsmith_tracing() is False
    assert langsmith.os.environ["LANGSMITH_PROJECT"] == "autobrowser"
    assert langsmith.os.environ["LANGCHAIN_PROJECT"] == "autobrowser"


def test_resolve_task_prefers_positional() -> None:
    args = make_args(task=["open", "site"], task_text="ignored")

    assert resolve_task(args) == "open site"


def test_start_chrome_skips_when_port_open(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_port_open", lambda port: True)

    assert start_chrome_cdp("chrome.exe", "profile", 9222) is None


def test_start_chrome_launches_when_port_closed(monkeypatch) -> None:
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str]) -> None:
            calls.append(command)

    monkeypatch.setattr(chrome, "is_port_open", lambda port: False)
    monkeypatch.setattr(chrome.subprocess, "Popen", FakePopen)

    process = start_chrome_cdp("chrome.exe", "profile", 9333)

    assert isinstance(process, FakePopen)
    assert calls[0][0] == "chrome.exe"
    assert "--remote-debugging-port=9333" in calls[0]
    assert "--user-data-dir=profile" in calls[0]


@pytest.mark.asyncio
async def test_load_browser_provider_wraps_raw_playwright_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = [FakeTool("browser_snapshot")]

    async def fake_load_browser_tools(port: int) -> list[FakeTool]:
        assert port == 9777
        return tools

    monkeypatch.setattr(playwright_runtime, "load_browser_tools", fake_load_browser_tools)

    provider = await playwright_runtime.load_browser_provider(9777)

    assert isinstance(provider, PlaywrightMCPBrowserProvider)
    assert await provider.get_tools() == tools


@pytest.mark.asyncio
async def test_setup_mcp_uses_latest_playwright_mcp(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeMCPClient:
        def __init__(self, servers: dict[str, Any]) -> None:
            captured["servers"] = servers

        async def get_tools(self) -> list[Any]:
            return []

    monkeypatch.setenv("PORT", "9777")
    monkeypatch.setattr(mcp_setup, "MultiServerMCPClient", FakeMCPClient)

    await mcp_setup.setup_mcp()

    args = captured["servers"]["browser"]["args"]
    assert args[:2] == ["-y", "@playwright/mcp@latest"]
    assert "mcp-server-playwright" not in args
    assert "http://localhost:9777" in args


@pytest.mark.asyncio
async def test_run_agent_prints_final_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    async def task_runner(
        _harness: Any,
        task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        return native_result(final_answer=f"done: {task}")

    install_runner(monkeypatch, task_runner)
    exit_session_after_initial_task(monkeypatch)
    args = make_args()

    exit_code = await bootstrap.run_agent(args)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "done: inspect page" in output
    assert "Interactive mode" in output
    assert langsmith.os.environ["LANGSMITH_PROJECT"] == "autobrowser"


@pytest.mark.asyncio
async def test_run_agent_keeps_session_alive_for_prompted_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    prompts = iter(["second task", "quit"])

    async def task_runner(
        _harness: Any,
        task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        return native_result(final_answer=f"done: {task}")

    install_runner(monkeypatch, task_runner)
    build_session_with(monkeypatch, input_fn=lambda _prompt="": next(prompts))

    exit_code = await bootstrap.run_agent(make_args(task=["first", "task"]))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count("done:") == 2
    assert "done: first task" in output
    assert "done: second task" in output


@pytest.mark.asyncio
async def test_no_mcp_does_not_start_chrome_or_load_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_start(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Chrome should not start in --no-mcp mode")

    async def fail_load(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("MCP should not load in --no-mcp mode")

    async def task_runner(
        _harness: Any,
        _task: str,
        _config: Any,
        _task_config: dict[str, Any],
    ) -> AgentLoopResult:
        return native_result(final_answer="done")

    install_runner(monkeypatch, task_runner)
    build_session_with(
        monkeypatch,
        start_chrome=fail_start,
        browser_provider_loader=fail_load,
        input_fn=lambda _prompt="": "quit",
    )

    exit_code = await bootstrap.run_agent(make_args(no_mcp=True))

    assert exit_code == 0


@pytest.mark.asyncio
async def test_mcp_mode_starts_chrome_and_passes_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    events: list[Any] = []
    tools = [FakeTool("browser_snapshot"), FakeTool("custom_tool")]

    def fake_start(chrome_path: str, user_data_dir: str, port: int) -> None:
        events.append(("start", chrome_path, user_data_dir, port))

    async def fake_wait(port: int, timeout: float) -> None:
        events.append(("wait", port, timeout))

    async def fake_load(port: int) -> FakeBrowserProvider:
        events.append(("load", port))
        return FakeBrowserProvider(tools)

    def fake_print_tools(loaded: list[Any]) -> None:
        events.append(("print_tools", loaded))

    session = bootstrap.build_session(
        make_args(no_mcp=False, cdp_port=9555, show_tools=True),
        llm_factory=llm_factory,
        start_chrome=fake_start,
        wait_for_cdp_port=fake_wait,
        browser_provider_loader=fake_load,
        tool_printer=fake_print_tools,
        tracing_configurator=lambda: False,
    )
    await session.start()

    assert ("start", "chrome.exe", "profile", 9555) in events
    assert ("wait", 9555, 1) in events
    assert ("load", 9555) in events
    assert any(event[0] == "print_tools" for event in events)
    registry = session.context.tool_registry
    assert registry is not None
    assert await registry.get_all() == tools
    assert len(registry.get_browser_providers()) == 1
    assert session.harness.compress_tools is False


@pytest.mark.asyncio
async def test_run_agent_passes_agent_loop_flag_into_session(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class FakeSession:
        async def run_forever(self, *, initial_task: str | None = None) -> int:
            _ = initial_task
            return 0

        async def close(self) -> None:
            return None

    def fake_build_session(args: argparse.Namespace, **kwargs: Any) -> FakeSession:
        seen["agent_loop"] = args.agent_loop
        return FakeSession()

    monkeypatch.setattr(bootstrap, "build_session", fake_build_session)

    exit_code = await bootstrap.run_agent(make_args(agent_loop=True))

    assert exit_code == 0
    assert seen["agent_loop"] is True
