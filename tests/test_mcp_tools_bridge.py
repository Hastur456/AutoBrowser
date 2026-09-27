"""MCP Manager -> harness tools -> ToolBroker, end to end over a real stdio server."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from mcp import types

from src.agent_loop.execution.tools import ToolBroker
from src.browser.normalization import BrowserToolNormalizer
from src.harness.mcp_tools import MCPToolSource, call_tool_result_to_text
from src.mcp import MCPManager

FAKE_SERVER = str(Path(__file__).parent / "mcp_fixtures" / "fake_server.py")


def fake(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [FAKE_SERVER],
        "reconnect": {"base_delay_s": 0.05},
    }
    config.update(overrides)
    return config


class _Registry:
    """Minimal stand-in for ToolRegistry: ``get() -> {name: tool}``."""

    def __init__(self, source: MCPToolSource) -> None:
        self._source = source

    async def get(self) -> dict[str, Any]:
        return {tool.name: tool for tool in await self._source.get_tools()}


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


def test_result_flattening() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="line 1"),
            types.ImageContent(type="image", data="QUJD", mimeType="image/png"),
            types.TextContent(type="text", text="line 2"),
        ]
    )
    assert call_tool_result_to_text(result) == "line 1\n[image: image/png, 4 base64 chars]\nline 2"
    structured = types.CallToolResult(content=[], structuredContent={"a": 1})
    assert call_tool_result_to_text(structured) == '{"a": 1}'


def test_exposure_prefixed_and_unprefixed() -> None:
    async def scenario() -> None:
        async with MCPManager({"browser": fake(), "other": fake()}) as manager:
            source = MCPToolSource(manager, unprefixed_servers=["browser"])
            names = {t.name for t in await source.get_tools()}
            assert {"echo", "increment", "other__echo", "other__increment"} <= names
            assert "browser__echo" not in names and "other__get_user" in names
            # dotted MCP name is not provider-safe -> falls back to the qualified name
            assert "browser__get_user" in names and "get.user" not in names
            by_name = {t.name: t for t in await source.get_tools()}
            assert by_name["echo"].qualified_name == "browser__echo"
            assert by_name["echo"].args == {"text": by_name["echo"].input_schema["properties"]["text"]}

    run(scenario())


def test_broker_success_tool_error_and_connection_lost_codes() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake(stateful=True)}) as manager:
            broker = ToolBroker(_Registry(MCPToolSource(manager)), normalizers=[BrowserToolNormalizer()])

            ok = await broker.execute({"name": "fake__echo", "args": {"text": "hi"}}, {})
            assert ok == {"name": "fake__echo", "status": "success", "content": "hi", "error": ""}

            failed = await broker.execute({"name": "fake__fail", "args": {"message": "boom"}}, {})
            assert failed["status"] == "error" and "boom" in failed["error"]
            assert "error_code" not in failed

            lost = await broker.execute({"name": "fake__crash", "args": {}}, {})
            assert lost["status"] == "error" and lost["error_code"] == "mcp_connection_lost"
            assert "state is lost" in lost["error"]

            unknown = await broker.execute({"name": "fake__nope", "args": {}}, {})
            assert unknown["status"] == "error" and "Unknown tool" in unknown["error"]

    run(scenario())


def test_list_changed_reaches_the_broker() -> None:
    async def scenario() -> None:
        async with MCPManager({"fake": fake()}) as manager:
            source = MCPToolSource(manager)
            broker = ToolBroker(_Registry(source))
            await broker.execute({"name": "fake__add_tool", "args": {"name": "late"}}, {})
            for _ in range(100):
                if any(t.name == "fake__late" for t in source.tools()):
                    break
                await asyncio.sleep(0.05)
            late = await broker.execute({"name": "fake__late", "args": {}}, {})
            assert late["status"] == "success" and late["content"] == "dynamic:late"

    run(scenario())
