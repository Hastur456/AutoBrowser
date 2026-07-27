from __future__ import annotations

import argparse
from typing import Any

import pytest

import main
from src.browser import PlaywrightMCPBrowserProvider
from src.mcp import mcp_setup, playwright_runtime


class FakeChatOllama:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


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


class FakeGraph:
    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        return {
            "task": state["task"],
            "final_answer": f"done: {state['task']}",
            "config": config,
        }

    async def astream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str,
    ):
        yield {"plan": {"task": state["task"], "stream_mode": stream_mode}}
        yield {
            "observe": {
                "tool_result": {
                    "name": "browser_snapshot",
                    "status": "success",
                    "content": '- textbox "Search" ref=e8',
                    "error": "",
                },
                "observation": 'browser_snapshot returned success.\n\n- textbox "Search" ref=e8',
                "snapshot": '- textbox "Search" ref=e8',
                "config": config,
            }
        }
        yield {"agent": {"final_answer": f"done: {state['task']}", "config": config}}


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
        "chrome_path": "chrome.exe",
        "user_data_dir": "profile",
        "cdp_port": 9222,
        "cdp_timeout": 1,
        "recursion_limit": 3,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def exit_session_after_initial_task(monkeypatch) -> None:
    monkeypatch.setattr(main, "input", lambda _prompt="": "quit", raising=False)


def test_parser_accepts_cli_flags() -> None:
    parser = main.build_parser()

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
    assert args.chrome_path == "chrome.exe"
    assert args.user_data_dir == "profile"
    assert args.cdp_port == 9333
    assert args.cdp_timeout == 5
    assert args.recursion_limit == 7


def test_format_state_json() -> None:
    assert main.format_state({"text": "привет"}, as_json=True) == '{\n  "text": "привет"\n}'


def test_print_tools(capsys) -> None:
    main.print_tools([FakeTool("browser_navigate"), FakeTool("browser_snapshot")])

    output = capsys.readouterr().out
    assert "MCP tools:" in output
    assert "- browser_navigate" in output
    assert "- browser_snapshot" in output


def test_configure_langsmith_tracing_enables_legacy_vars(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "browser-runs")
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    assert main.configure_langsmith_tracing() is True
    assert main.os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert main.os.environ["LANGCHAIN_PROJECT"] == "browser-runs"


def test_configure_langsmith_tracing_sets_default_project(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    assert main.configure_langsmith_tracing() is False
    assert main.os.environ["LANGSMITH_PROJECT"] == "autobrowser"
    assert main.os.environ["LANGCHAIN_PROJECT"] == "autobrowser"


def test_resolve_task_prefers_positional() -> None:
    args = make_args(task=["open", "site"], task_text="ignored")

    assert main.resolve_task(args) == "open site"


def test_start_chrome_skips_when_port_open(monkeypatch) -> None:
    monkeypatch.setattr(main, "is_port_open", lambda port: True)

    assert main.start_chrome_cdp("chrome.exe", "profile", 9222) is None


def test_start_chrome_launches_when_port_closed(monkeypatch) -> None:
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str]) -> None:
            calls.append(command)

    monkeypatch.setattr(main, "is_port_open", lambda port: False)
    monkeypatch.setattr(main.subprocess, "Popen", FakePopen)

    process = main.start_chrome_cdp("chrome.exe", "profile", 9333)

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
async def test_run_agent_prints_final_answer(monkeypatch, capsys) -> None:
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "build_agent_graph", lambda **kwargs: FakeGraph())
    exit_session_after_initial_task(monkeypatch)
    args = make_args()

    exit_code = await main.run_agent(args)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "done: inspect page" in output
    assert "Interactive mode" in output
    assert main.os.environ["LANGSMITH_PROJECT"] == "autobrowser"


@pytest.mark.asyncio
async def test_run_agent_prints_node_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "build_agent_graph", lambda **kwargs: FakeGraph())
    exit_session_after_initial_task(monkeypatch)
    args = make_args(task=["inspect"], show_state=True)

    exit_code = await main.run_agent(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "[PLAN]" in output
    assert "[AGENT]" in output


@pytest.mark.asyncio
async def test_run_agent_can_hide_snapshots_from_node_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "build_agent_graph", lambda **kwargs: FakeGraph())
    exit_session_after_initial_task(monkeypatch)
    args = make_args(task=["inspect"], show_state=True, hide_snapshot=True)

    exit_code = await main.run_agent(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "[OBSERVE]" in output
    assert "[browser_snapshot hidden]" in output
    assert 'textbox "Search"' not in output
    assert "final_answer" in output


@pytest.mark.asyncio
async def test_no_mcp_does_not_start_chrome_or_load_tools(monkeypatch) -> None:
    def fail_start(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Chrome should not start in --no-mcp mode")

    async def fail_load_tools(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("MCP should not load in --no-mcp mode")

    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "build_agent_graph", lambda **kwargs: FakeGraph())
    monkeypatch.setattr(main, "start_chrome_cdp", fail_start)
    monkeypatch.setattr(main, "load_browser_provider", fail_load_tools)
    exit_session_after_initial_task(monkeypatch)

    exit_code = await main.run_agent(make_args(no_mcp=True))

    assert exit_code == 0


@pytest.mark.asyncio
async def test_mcp_mode_starts_chrome_and_passes_tools(monkeypatch) -> None:
    events: list[Any] = []
    tools = [FakeTool("browser_snapshot"), FakeTool("custom_tool")]

    def fake_start(chrome_path: str, user_data_dir: str, port: int) -> None:
        events.append(("start", chrome_path, user_data_dir, port))

    async def fake_wait(port: int, timeout: float) -> None:
        events.append(("wait", port, timeout))

    async def fake_load(port: int) -> FakeBrowserProvider:
        events.append(("load", port))
        return FakeBrowserProvider(tools)

    def fake_build_agent_graph(**kwargs: Any) -> FakeGraph:
        events.append(("tool_registry", kwargs["tool_registry"]))
        events.append(("policy_node", kwargs["policy_node"]))
        events.append(("history_builder", kwargs["history_builder"]))
        events.append(("compress_tools", kwargs["compress_tools"]))
        return FakeGraph()

    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "start_chrome_cdp", fake_start)
    monkeypatch.setattr(main, "wait_for_port", fake_wait)
    monkeypatch.setattr(main, "load_browser_provider", fake_load)
    monkeypatch.setattr(main, "build_agent_graph", fake_build_agent_graph)
    exit_session_after_initial_task(monkeypatch)

    exit_code = await main.run_agent(
        make_args(no_mcp=False, cdp_port=9555, show_tools=True)
    )

    assert exit_code == 0
    assert ("start", "chrome.exe", "profile", 9555) in events
    assert ("wait", 9555, 1) in events
    assert ("load", 9555) in events
    registry = next(event[1] for event in events if event[0] == "tool_registry")
    assert await registry.get_all() == tools
    assert len(registry.get_browser_providers()) == 1
    assert any(
        event[0] == "policy_node" and callable(event[1])
        for event in events
    )
    assert any(
        event[0] == "history_builder" and callable(event[1])
        for event in events
    )
    assert ("compress_tools", False) in events


@pytest.mark.asyncio
async def test_run_agent_keeps_session_alive_for_prompted_task(monkeypatch, capsys) -> None:
    graphs: list[FakeGraph] = []
    prompts = iter(["second task", "quit"])

    def fake_build_agent_graph(**_kwargs: Any) -> FakeGraph:
        graph = FakeGraph()
        graphs.append(graph)
        return graph

    monkeypatch.setattr(main, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(main, "build_agent_graph", fake_build_agent_graph)
    monkeypatch.setattr(main, "input", lambda _prompt="": next(prompts), raising=False)

    exit_code = await main.run_agent(make_args(task=["first", "task"]))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count("done:") == 2
    assert "done: first task" in output
    assert "done: second task" in output
    assert len(graphs) == 1
