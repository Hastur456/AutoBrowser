"""Browser tool-call normalization, independent of any MCP server or adapter.

Tools come from the MCP Manager via the tool registry, under the names the server exposes,
so what remains is a pure, stateless request normalizer: it drops arguments the tool's JSON
schema forbids (``additionalProperties: false``). Names are never translated, arguments are
otherwise passed through unchanged (no element-reference rewriting, no snapshot lookups),
and results are passed through unchanged.

The broker hands in the current ``{name: tool}`` map, so the normalizer holds no tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from src.browser.names import is_browser_tool_name
from src.contracts import ToolRequest, ToolResult


@runtime_checkable
class ToolCallNormalizer(Protocol):
    """Folded by ``ToolBroker`` around every tool call (request before, result after).

    ``state`` is the read-only loop state mapping, ``tools`` the current ``{name: tool}``
    map of the registry.
    """

    def normalize_request(
        self,
        request: ToolRequest,
        state: Mapping[str, Any],
        tools: Mapping[str, Any],
    ) -> ToolRequest: ...

    def normalize_result(self, result: ToolResult) -> ToolResult: ...


def tool_input_schema(tool: Any) -> dict[str, Any]:
    """Best-effort JSON schema of a tool object (MCP tools expose ``input_schema``)."""

    for attr in ("input_schema", "args_schema"):
        schema = getattr(tool, attr, None)
        if schema is None:
            continue
        if isinstance(schema, Mapping):
            return dict(schema)
        if hasattr(schema, "model_json_schema"):
            return schema.model_json_schema()
    args = getattr(tool, "args", None)
    if isinstance(args, Mapping):
        return dict(args) if "properties" in args else {"properties": dict(args)}
    return {}


class BrowserToolNormalizer(ToolCallNormalizer):
    """Stateless browser normalizer usable with any browser-automation MCP server."""

    def normalize_request(
        self,
        request: ToolRequest,
        state: Mapping[str, Any],
        tools: Mapping[str, Any],
    ) -> ToolRequest:
        normalized: dict[str, Any] = dict(request)
        args = dict(request.get("args") or {})
        requested = str(request.get("name", "") or "")
        normalized["args"] = args
        if not is_browser_tool_name(requested):
            return normalized  # type: ignore[return-value]

        tool = tools.get(requested)
        if tool is None:
            return normalized  # type: ignore[return-value]

        schema = tool_input_schema(tool)
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and schema.get("additionalProperties") is False:
            normalized["args"] = {key: value for key, value in args.items() if key in properties}
        return normalized  # type: ignore[return-value]

    def normalize_result(self, result: ToolResult) -> ToolResult:
        return dict(result)  # type: ignore[return-value]


__all__ = [
    "BrowserToolNormalizer",
    "ToolCallNormalizer",
    "tool_input_schema",
]
