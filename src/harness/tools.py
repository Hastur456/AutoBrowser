"""Pluggable tool registry for harness-managed tool injection."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from src.contracts import Tool, ToolDef
from src.harness.normalization import ToolCallNormalizer, as_normalizers

ToolCollection = Sequence[Any]
ToolLoadResult = ToolCollection | Awaitable[ToolCollection]
ToolLoader = Callable[[], ToolLoadResult]


@runtime_checkable
class MCPToolClient(Protocol):
    """Protocol for clients that can expose a collection of tools (loaded once)."""

    async def get_tools(self) -> ToolCollection:
        """Return the tools exposed by the client."""


@runtime_checkable
class LiveToolSource(Protocol):
    """A source whose tool list changes at runtime (e.g. :class:`MCPToolSource`).

    ``version`` changes whenever ``get_tools()`` may return something different; the
    registry re-reads the source only then. MCP ``list_changed`` rediscovery and servers
    dropping out / coming back are therefore visible without rebuilding the registry.
    """

    @property
    def version(self) -> int:
        """Monotonic catalog version."""

    async def get_tools(self) -> ToolCollection:
        """Return the tools currently exposed by the source."""


ToolSource = LiveToolSource | MCPToolClient
ToolProvider = ToolCollection | ToolLoader | ToolSource


def tool_name(tool: Any) -> str:
    """Return the stable name used to bind and execute a tool."""

    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def tool_is_read_only(tool: Any) -> bool:
    """True when the tool's server declared the MCP ``readOnlyHint`` annotation.

    Protocol-level metadata, not a name list: a read-only tool cannot change the
    environment, so state observed before the call stays valid after it. Tools without
    annotations are conservatively treated as state-changing.
    """

    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    if isinstance(annotations, Mapping):
        value = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    else:
        value = getattr(annotations, "readOnlyHint", None)
    return value is True


def to_tool_def(tool: Any) -> ToolDef:
    """Extract the model-visible ``ToolDef`` schema from a registered tool.

    Registry tools are provider-neutral :class:`~src.contracts.Tool` objects whose
    ``to_def()`` already yields the schema; plain duck-typed callables (``name``/
    ``description`` plus ``input_schema`` or a pydantic-style ``args_schema``) are
    still accepted directly — MCP tools (:class:`~src.harness.mcp_tools.MCPTool`)
    expose ``input_schema`` as a JSON-schema dict.
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
    """Registry for direct tool lists, one-shot providers and live tool sources.

    * ``tools`` / one-shot ``providers`` / ``tool_loader`` are loaded at most once;
    * :class:`LiveToolSource` providers are re-read whenever their ``version`` changes;
    * ``normalizers`` are the request/result normalizers that apply to these tools
      (``ToolBroker`` uses them when it is not given its own).
    """

    def __init__(
        self,
        tools: ToolCollection | None = None,
        providers: Iterable[ToolProvider] | None = None,
        tool_loader: ToolLoader | None = None,
        normalizers: ToolCallNormalizer | Iterable[ToolCallNormalizer] | None = None,
    ) -> None:
        self._tools = list(tools) if tools is not None else None
        self._pending: list[ToolProvider] = []
        self._live: list[LiveToolSource] = []
        for provider in providers or []:
            if isinstance(provider, LiveToolSource):
                self._live.append(provider)
            else:
                self._pending.append(provider)
        if tool_loader is not None:
            self._pending.append(tool_loader)
        self._live_cache: dict[int, tuple[int, list[Any]]] = {}
        self._normalizers = as_normalizers(normalizers)

    async def get_all(self) -> list[Any]:
        """Return all registered tools: static ones plus the current live ones."""

        if self._tools is None:
            self._tools = []

        while self._pending:
            provider = self._pending.pop(0)
            self._tools.extend(await self._load_provider(provider))

        live: list[Any] = []
        for source in self._live:
            version = source.version
            cached = self._live_cache.get(id(source))
            if cached is None or cached[0] != version:
                cached = (version, list(await self._resolve(source.get_tools())))
                self._live_cache[id(source)] = cached
            live.extend(cached[1])

        return [*self._tools, *live]

    async def get_by_name(self) -> dict[str, Any]:
        """Return registered tools keyed by their execution name."""

        return {tool_name(tool): tool for tool in await self.get_all() if tool_name(tool)}

    async def get(self) -> dict[str, Any]:
        """Compatibility alias for executor code that expects a name map."""

        return await self.get_by_name()

    def get_normalizers(self) -> list[ToolCallNormalizer]:
        """Return the tool-call normalizers registered with these tools."""

        return list(self._normalizers)

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
    "LiveToolSource",
    "MCPToolClient",
    "ToolCollection",
    "ToolLoader",
    "ToolLoadResult",
    "ToolProvider",
    "ToolSource",
    "ToolRegistry",
    "to_tool_def",
    "tool_is_read_only",
    "tool_name",
]