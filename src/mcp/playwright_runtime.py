"""Playwright MCP runtime lifecycle helpers."""

from __future__ import annotations

from contextlib import AsyncExitStack
import os
from typing import Any

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.browser import BrowserProvider, PlaywrightMCPBrowserProvider


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


async def load_browser_tools(port: int) -> list[Any]:
    """Load Playwright MCP tools from a direct ClientSession."""

    os.environ["PORT"] = str(port)
    session = await get_mcp_session(port)
    return list(await load_mcp_tools(session))


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
