"""Bridge: MCP Manager catalog -> harness tool objects.

The manager returns raw descriptors and ``CallToolResult``; this module is the harness-side
adapter that turns them into invocable tool objects for :class:`~src.harness.tools.ToolRegistry`
/ :class:`~src.agent_loop.execution.tools.ToolBroker` (duck-typed: ``name``, ``description``,
``input_schema``/``args_schema``, async ``invoke(args)``).

Naming. By default a tool is exposed under its manager-qualified name
(``server__tool``). Servers listed in ``unprefixed_servers`` expose their tools under the
server's own (sanitized) names — this is how the browser server keeps its own
``browser_*`` names that observation, evals and golden traces rely on. A local
name that is not provider-safe or collides with another exposed name falls back to the
qualified name, so exposure is always unique and deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp import types

from src.mcp import MCPManager, ToolDescriptor
from src.mcp.naming import is_provider_safe


class MCPToolExecutionError(RuntimeError):
    """The tool ran and reported failure (``CallToolResult.isError``).

    Raised so the broker's ``except`` turns it into ``status="error"`` exactly as tool
    exceptions were handled before; carries the raw result for callers that want it.
    """

    error_code = ""  # no dedicated code: browser normalizers may classify the text

    def __init__(self, message: str, result: types.CallToolResult | None = None) -> None:
        super().__init__(message or "Tool reported an error.")
        self.result = result


def call_tool_result_to_text(result: types.CallToolResult) -> str:
    """Flatten MCP content blocks into the text the agent loop observes.

    Text blocks are joined with newlines (Playwright MCP puts page state/snapshots in text).
    Binary blocks become short placeholders; ``structuredContent`` is used only when there
    is no content at all.
    """

    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            parts.append(f"[image: {block.mimeType}, {len(block.data)} base64 chars]")
        elif isinstance(block, types.AudioContent):
            parts.append(f"[audio: {block.mimeType}, {len(block.data)} base64 chars]")
        elif isinstance(block, types.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, types.TextResourceContents):
                parts.append(resource.text)
            else:
                parts.append(f"[resource: {resource.uri} ({resource.mimeType or 'binary'})]")
        elif isinstance(block, types.ResourceLink):
            parts.append(f"[resource link: {block.uri}]")
        else:  # future block types
            parts.append(str(getattr(block, "text", "") or block))
    if not parts and result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False, default=str)
    return "\n".join(parts)


@dataclass(frozen=True)
class MCPTool:
    """One MCP tool as the harness sees it."""

    name: str  # exposed name (what the LLM calls)
    qualified_name: str  # manager routing key
    server: str
    local_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None = None
    manager: MCPManager | None = field(default=None, repr=False, compare=False)

    # Aliases read by schema-aware code (the previous adapters looked at these names).
    @property
    def args_schema(self) -> dict[str, Any]:
        return self.input_schema

    @property
    def args(self) -> dict[str, Any]:
        properties = self.input_schema.get("properties", {})
        return properties if isinstance(properties, dict) else {}

    async def invoke_raw(self, args: Mapping[str, Any] | None = None, **kwargs: Any) -> types.CallToolResult:
        if self.manager is None:
            raise RuntimeError(f"MCP tool {self.name} is not bound to a manager")
        return await self.manager.call_tool(self.qualified_name, dict(args or {}), **kwargs)

    async def invoke(self, args: Mapping[str, Any] | None = None) -> str:
        """Call the tool; return its text, raise on ``isError`` or manager errors."""

        result = await self.invoke_raw(args)
        output = call_tool_result_to_text(result)
        if result.isError:
            raise MCPToolExecutionError(output, result)
        return output


class MCPToolSource:
    """Expose the manager's live catalog as harness tools.

    Same ``get_tools()`` shape the old ``BrowserProvider`` had, so it can be registered
    wherever the tool registry collects tools. The list is rebuilt from the in-memory
    catalog on every call (no I/O), so ``list_changed`` rediscovery and servers dropping
    out/coming back are reflected immediately; ``version`` lets callers cache.
    """

    def __init__(self, manager: MCPManager, *, unprefixed_servers: Iterable[str] = ()) -> None:
        self._manager = manager
        self._unprefixed = frozenset(unprefixed_servers)
        self._cache_version = -1
        self._cache: list[MCPTool] = []

    @property
    def manager(self) -> MCPManager:
        return self._manager

    @property
    def version(self) -> int:
        return self._manager.catalog_version

    def tools(self) -> list[MCPTool]:
        if self._cache_version != self._manager.catalog_version:
            self._cache = self._build(self._manager.list_tools())
            self._cache_version = self._manager.catalog_version
        return list(self._cache)

    async def get_tools(self) -> list[MCPTool]:
        return self.tools()

    def _build(self, descriptors: list[ToolDescriptor]) -> list[MCPTool]:
        ordered = sorted(descriptors, key=lambda item: item.qualified_name)
        qualified = {d.qualified_name for d in ordered}  # reserved: never shadowed
        local_counts: dict[str, int] = {}
        for d in ordered:
            if d.server in self._unprefixed:
                local_counts[d.local_name] = local_counts.get(d.local_name, 0) + 1

        def exposed_name(d: ToolDescriptor) -> str:
            if (
                d.server in self._unprefixed
                and local_counts.get(d.local_name) == 1
                and is_provider_safe(d.local_name)
                and d.local_name not in qualified
            ):
                return d.local_name
            return d.qualified_name

        return [
            MCPTool(
                name=exposed_name(d),
                qualified_name=d.qualified_name,
                server=d.server,
                local_name=d.local_name,
                description=d.description or d.title or "",
                input_schema=d.input_schema,
                annotations=d.annotations,
                manager=self._manager,
            )
            for d in ordered
        ]


__all__ = [
    "MCPTool",
    "MCPToolExecutionError",
    "MCPToolSource",
    "call_tool_result_to_text",
]
