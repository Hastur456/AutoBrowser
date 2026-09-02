"""Pluggable tool registry for harness-managed tool injection."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from src.browser.provider import BrowserProvider
from src.contracts import Tool, ToolDef

ToolCollection = Sequence[Any]
ToolLoadResult = ToolCollection | Awaitable[ToolCollection]
ToolLoader = Callable[[], ToolLoadResult]


@runtime_checkable
class MCPToolClient(Protocol):
    """Protocol for clients that can expose LangChain-compatible tools."""

    async def get_tools(self) -> ToolCollection:
        """Return the tools exposed by the client."""


ToolSource = MCPToolClient | BrowserProvider
ToolProvider = ToolCollection | ToolLoader | ToolSource


def tool_name(tool: Any) -> str:
    """Return the stable name used to bind and execute a tool."""

    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def to_tool_def(tool: Any) -> ToolDef:
    """Extract the model-visible ``ToolDef`` schema from a registered tool.

    Registry tools are provider-neutral :class:`~src.contracts.Tool` objects whose
    ``to_def()`` already yields the schema; plain duck-typed callables (``name``/
    ``description`` plus ``input_schema`` or a pydantic-style ``args_schema``) are
    still accepted without any langchain import.
    """

    if isinstance(tool, Tool):
        return tool.to_def()
    input_schema: Any = getattr(tool, "input_schema", None)
    if input_schema is None:
        args_schema = getattr(tool, "args_schema", None)
        model_json_schema = getattr(args_schema, "model_json_schema", None)
        if callable(model_json_schema):
            input_schema = model_json_schema()
    if input_schema is None:
        input_schema = {}
    return ToolDef(
        name=tool_name(tool),
        description=str(getattr(tool, "description", "") or ""),
        input_schema=input_schema if isinstance(input_schema, dict) else {},
    )


class ToolRegistry:
    """Lazy registry for direct tool lists, tool providers, and browser providers."""

    def __init__(
        self,
        tools: ToolCollection | None = None,
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
        """Return browser-provider adapters passed through ``providers``."""

        return [
            provider
            for provider in self._provider_refs
            if isinstance(provider, BrowserProvider)
        ]

    async def _load_provider(self, provider: ToolProvider) -> list[Any]:
        if isinstance(provider, MCPToolClient):
            return list(await self._resolve(provider.get_tools()))

        if isinstance(provider, Sequence) and not isinstance(provider, (str, bytes)):
            return list(provider)

        if callable(provider):
            return list(await self._resolve(provider()))

        get_tools = getattr(provider, "get_tools", None)
        if callable(get_tools):
            return list(await self._resolve(get_tools()))

        raise TypeError(f"Unsupported tool provider: {type(provider).__name__}")

    async def _resolve(self, value: ToolLoadResult) -> ToolCollection:
        result = value
        if inspect.isawaitable(result):
            result = await result
        return result


__all__ = [
    "MCPToolClient",
    "ToolCollection",
    "ToolLoader",
    "ToolLoadResult",
    "ToolProvider",
    "ToolSource",
    "ToolRegistry",
    "to_tool_def",
    "tool_name",
]
