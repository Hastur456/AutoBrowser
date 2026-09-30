"""``ToolBroker.prepare``/``invoke`` split: ``execute`` stays ``invoke(prepare())``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.execution.tools import PreparedToolCall, ToolBroker
from src.browser import FakeBrowserProvider
from src.browser.normalization import BrowserToolNormalizer
from src.contracts import Tool
from src.harness.mcp_tools import MCPToolSource
from src.harness.tools import ToolRegistry
from src.mcp import MCPManager

FAKE_SERVER = str(Path(__file__).parent / "mcp_fixtures" / "fake_server.py")


class _Registry:
    """Minimal stand-in for ToolRegistry: ``get() -> {name: tool}``."""

    def __init__(self, source: MCPToolSource) -> None:
        self._source = source

    async def get(self) -> dict[str, Any]:
        return {tool.name: tool for tool in await self._source.get_tools()}


def _counting_tool(calls: list[dict[str, Any]]) -> Tool:
    async def _echo(**kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return f"echo:{kwargs.get('text', '')}"

    return Tool(name="echo", func=_echo)


def _fake_browser_broker() -> ToolBroker:
    provider = FakeBrowserProvider(['- textbox "Search" ref=e8'])
    registry = ToolRegistry(providers=[provider], normalizers=[BrowserToolNormalizer(), provider])
    return ToolBroker(registry)


@pytest.mark.asyncio
async def test_execute_equals_invoke_of_prepare_for_a_plain_tool() -> None:
    calls: list[dict[str, Any]] = []
    broker = ToolBroker(ToolRegistry(tools=[_counting_tool(calls)]))
    request = {"name": "echo", "args": {"text": "hi"}}

    executed = await broker.execute(request, {})
    prepared = await broker.prepare(request, {})
    invoked = await broker.invoke(prepared)

    assert executed == invoked == {
        "name": "echo",
        "status": "success",
        "content": "echo:hi",
        "error": "",
    }
    assert calls == [{"text": "hi"}, {"text": "hi"}]


@pytest.mark.asyncio
async def test_prepare_does_not_run_the_tool_and_reports_no_server_for_a_plain_tool() -> None:
    calls: list[dict[str, Any]] = []
    tool = _counting_tool(calls)
    broker = ToolBroker(ToolRegistry(tools=[tool]))

    prepared = await broker.prepare({"name": "echo", "args": {"text": "hi"}})

    assert calls == []
    assert prepared.tool is tool
    assert prepared.server == ""
    assert prepared.error_result is None
    assert prepared.request == {"name": "echo", "args": {"text": "hi"}}


@pytest.mark.asyncio
async def test_prepare_normalizes_the_request_through_the_registry_normalizers() -> None:
    broker = _fake_browser_broker()

    prepared = await broker.prepare({"name": "browser.type", "args": {"ref": "e8", "text": "x"}})

    assert prepared.request["name"] == "browser_type"
    assert prepared.request["args"]["target"] == "e8"
    assert prepared.tool is not None
    assert prepared.server == ""  # FakeBrowserProvider tools are not MCP-backed


@pytest.mark.asyncio
async def test_preparing_an_already_prepared_request_changes_nothing() -> None:
    broker = _fake_browser_broker()

    first = await broker.prepare({"name": "browser.type", "args": {"ref": "e8", "text": "x"}})
    second = await broker.prepare(first.request)

    assert second.request == first.request
    assert second.tool is first.tool


@pytest.mark.asyncio
async def test_an_empty_name_prepares_an_error_result_that_invoke_returns() -> None:
    broker = ToolBroker(ToolRegistry(tools=[_counting_tool([])]))

    prepared = await broker.prepare({"name": "", "args": {}})

    assert prepared.tool is None
    assert prepared.error_result is not None
    assert await broker.invoke(prepared) == {
        "name": "",
        "status": "error",
        "content": "",
        "error": "No tool request was provided.",
    }
    assert await broker.execute({"name": "", "args": {}}) == await broker.invoke(prepared)


@pytest.mark.asyncio
async def test_an_unknown_tool_prepares_an_error_result_that_invoke_only_normalizes() -> None:
    calls: list[dict[str, Any]] = []
    broker = ToolBroker(ToolRegistry(tools=[_counting_tool(calls)]))

    prepared = await broker.prepare({"name": "nope", "args": {}})
    result = await broker.invoke(prepared)

    assert prepared.tool is None and prepared.server == ""
    assert result["status"] == "error"
    assert result["error"] == "Unknown tool: nope. Available tools: echo"
    assert calls == []
    assert await broker.execute({"name": "nope", "args": {}}) == result


@pytest.mark.asyncio
async def test_invoke_runs_the_prepared_request_not_a_new_lookup() -> None:
    calls: list[dict[str, Any]] = []
    tool = _counting_tool(calls)
    broker = ToolBroker(ToolRegistry(tools=[tool]))
    prepared = PreparedToolCall(request={"name": "echo", "args": {"text": "direct"}}, tool=tool, server="")

    result = await broker.invoke(prepared)

    assert result["content"] == "echo:direct"
    assert calls == [{"text": "direct"}]


def test_prepare_reports_the_mcp_server_and_execute_matches_invoke() -> None:
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [FAKE_SERVER],
        "reconnect": {"base_delay_s": 0.05},
        "stateful": True,
    }

    async def scenario() -> None:
        async with MCPManager({"fake": config}) as manager:
            broker = ToolBroker(_Registry(MCPToolSource(manager)))

            prepared = await broker.prepare({"name": "fake__increment", "args": {}})
            assert prepared.server == "fake"
            assert prepared.tool is not None

            first = await broker.invoke(prepared)
            second = await broker.execute({"name": "fake__increment", "args": {}})
            assert first["status"] == second["status"] == "success"
            assert first["name"] == second["name"] == "fake__increment"
            assert first["content"] != second["content"]  # the counter really ran twice

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
