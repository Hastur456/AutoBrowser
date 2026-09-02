"""Playwright MCP runtime lifecycle helpers."""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.browser import BrowserProvider, PlaywrightMCPBrowserProvider
from src.contracts import Tool


class MCPRuntime:
    """Keep the stdio MCP process and ClientSession alive while tools run."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def start(self) -> ClientSession:
        params = StdioServerParameters(
            command="npx",
            args=[
                "-y",
                "@playwright/mcp@latest",
                "--cdp-endpoint",
                f"http://localhost:{self.port}",
            ],
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.session = session
        return session

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        except GeneratorExit:
            pass
        finally:
            self.session = None
            self._stack = AsyncExitStack()


_mcp_runtime: MCPRuntime | None = None


async def get_mcp_session(port: int) -> ClientSession:
    """Create or reuse a direct Playwright MCP ClientSession."""

    global _mcp_runtime
    if _mcp_runtime is None or _mcp_runtime.port != port:
        if _mcp_runtime is not None:
            await _mcp_runtime.close()
        os.environ["PORT"] = str(port)
        _mcp_runtime = MCPRuntime(port)
        return await _mcp_runtime.start()

    if _mcp_runtime.session is None:
        return await _mcp_runtime.start()
    return _mcp_runtime.session


async def close_mcp_session() -> None:
    """Close the direct MCP session and stdio server process."""

    global _mcp_runtime
    if _mcp_runtime is not None:
        await _mcp_runtime.close()
        _mcp_runtime = None


async def load_browser_tools(port: int) -> list[Tool]:
    """Load Playwright MCP tools as neutral ``Tool`` objects from the ClientSession.

    Replaces ``langchain_mcp_adapters.load_mcp_tools``: the direct MCP session's
    ``list_tools()``/``call_tool()`` drive each ``Tool``, so no langchain import is
    needed. Tool schemas are preserved verbatim from the server's ``inputSchema``.
    """

    os.environ["PORT"] = str(port)
    session = await get_mcp_session(port)
    listed = await session.list_tools()
    mcp_tools = getattr(listed, "tools", listed) or []
    return [_mcp_tool(session, mcp_tool) for mcp_tool in mcp_tools]


def _mcp_tool(session: ClientSession, mcp_tool: Any) -> Tool:
    """Wrap one raw MCP tool definition into a neutral ``Tool`` bound to ``session``."""

    name = str(getattr(mcp_tool, "name", "") or "")
    raw_schema = getattr(mcp_tool, "inputSchema", None)
    input_schema = raw_schema if isinstance(raw_schema, dict) else {}

    async def invoke(**kwargs: Any) -> str:
        result = await session.call_tool(name, arguments=dict(kwargs))
        content = _mcp_content_to_text(result)
        if getattr(result, "isError", False):
            raise RuntimeError(content or f"MCP tool {name} failed.")
        return content

    return Tool(
        name=name,
        description=str(getattr(mcp_tool, "description", "") or ""),
        input_schema=input_schema,
        func=invoke,
    )


def _mcp_content_to_text(result: Any) -> str:
    """Join an MCP ``CallToolResult`` content list into a single string."""

    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            text = str(getattr(block, "text", "") or "")
            if text:
                parts.append(text)
            continue
        try:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
        except TypeError:
            parts.append(str(block))
    return "\n".join(parts)


async def load_browser_provider(port: int) -> BrowserProvider:
    """Load Playwright MCP tools and wrap them in the browser adapter."""

    tools = await load_browser_tools(port)
    return PlaywrightMCPBrowserProvider(tools)


__all__ = [
    "MCPRuntime",
    "close_mcp_session",
    "get_mcp_session",
    "load_browser_provider",
    "load_browser_tools",
]
