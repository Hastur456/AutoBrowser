"""Pluggable tool registry for harness-managed tool injection."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from src.browser.provider import BrowserProvider

ToolLoader = Callable[[], Awaitable[Sequence[Any]]]
ToolProvider = Sequence[Any] | ToolLoader | Any


@runtime_checkable
class MCPToolClient(Protocol):
    """Protocol for clients that can expose LangChain-compatible tools."""

    async def get_tools(self) -> Sequence[Any]:
        """Return the tools exposed by the client."""


def tool_name(tool: Any) -> str:
    """Return the stable name used to bind and execute a tool."""

    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


class ToolRegistry:
    """Lazy registry for tools supplied by generic toolsets or MCP clients."""

    def __init__(
        self,
        tools: Sequence[Any] | None = None,
        providers: Iterable[ToolProvider] | None = None,
        tool_loader: ToolLoader | None = None,
    ) -> None:
        self._tools = list(tools) if tools is not None else None
        self._provider_refs = list(providers or [])
        self._providers = list(self._provider_refs)
        if tool_loader is not None:
            self._providers.append(tool_loader)

    async def get_all(self) -> list[Any]:
        """Return all registered tools, loading providers at most once."""

        if self._tools is None:
            self._tools = []

        while self._providers:
            provider = self._providers.pop(0)
            self._tools.extend(await self._load_provider(provider))

        return list(self._tools)

    async def get_by_name(self) -> dict[str, Any]:
        """Return registered tools keyed by their execution name."""

        return {tool_name(tool): tool for tool in await self.get_all() if tool_name(tool)}

    async def get(self) -> dict[str, Any]:
        """Compatibility alias for executor code that expects a name map."""

        return await self.get_by_name()

    def get_browser_providers(self) -> list[BrowserProvider]:
        """Return registered browser-provider adapters."""

        return [
            provider
            for provider in self._provider_refs
            if isinstance(provider, BrowserProvider)
        ]

    async def _load_provider(self, provider: ToolProvider) -> list[Any]:
        if isinstance(provider, Sequence) and not isinstance(provider, (str, bytes)):
            return list(provider)

        if callable(provider):
            return list(await self._resolve(provider()))

        get_tools = getattr(provider, "get_tools", None)
        if callable(get_tools):
            return list(await self._resolve(get_tools()))

        raise TypeError(f"Unsupported tool provider: {type(provider).__name__}")

    async def _resolve(self, value: Any) -> Sequence[Any]:
        result = value
        if inspect.isawaitable(result):
            result = await result
        return result


__all__ = [
    "MCPToolClient",
    "ToolLoader",
    "ToolProvider",
    "ToolRegistry",
    "tool_name",
]
