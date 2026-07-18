from __future__ import annotations

from typing import Any

import pytest

from src.harness.tools import ToolRegistry


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeMCPClient:
    async def get_tools(self) -> list[Any]:
        return [FakeTool("client_tool")]


@pytest.mark.asyncio
async def test_tool_registry_returns_direct_tools() -> None:
    registry = ToolRegistry(tools=[FakeTool("direct_tool")])

    tools = await registry.get_all()
    tools_by_name = await registry.get_by_name()

    assert [tool.name for tool in tools] == ["direct_tool"]
    assert sorted(tools_by_name) == ["direct_tool"]


@pytest.mark.asyncio
async def test_tool_registry_loads_generic_mcp_client() -> None:
    registry = ToolRegistry(providers=[FakeMCPClient()])

    tools_by_name = await registry.get_by_name()

    assert sorted(tools_by_name) == ["client_tool"]


@pytest.mark.asyncio
async def test_tool_registry_loads_callable_provider_once() -> None:
    calls = 0

    async def load_tools() -> list[Any]:
        nonlocal calls
        calls += 1
        return [FakeTool("loaded_tool")]

    registry = ToolRegistry(providers=[load_tools])

    assert sorted(await registry.get_by_name()) == ["loaded_tool"]
    assert sorted(await registry.get_by_name()) == ["loaded_tool"]
    assert calls == 1
