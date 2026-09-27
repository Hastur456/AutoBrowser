"""Tool-call normalization for the harness (server-neutral).

``ToolBroker`` folds every tool call through the registered :class:`ToolCallNormalizer`
objects: ``normalize_request`` before the call, ``normalize_result`` after it. The broker
hands in the current ``{name: tool}`` map, so normalizers hold no tools.

:class:`SchemaArgsNormalizer` is the only built-in one: it drops arguments that the tool's
JSON schema forbids (``additionalProperties: false``), for any tool of any MCP server.
There is no server-specific vocabulary here — no canonical tool names, no element refs,
no error-text parsing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

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


def as_normalizers(
    value: ToolCallNormalizer | Iterable[ToolCallNormalizer] | None,
) -> list[ToolCallNormalizer]:
    """Accept ``None``, one normalizer or an iterable of them; always return a list."""

    if value is None:
        return []
    if callable(getattr(value, "normalize_request", None)) and callable(
        getattr(value, "normalize_result", None)
    ):
        return [value]  # type: ignore[list-item]
    return [item for item in value if item is not None]  # type: ignore[union-attr]


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


class SchemaArgsNormalizer:
    """Drop arguments the target tool's schema forbids; pass everything else through."""

    def normalize_request(
        self,
        request: ToolRequest,
        state: Mapping[str, Any],
        tools: Mapping[str, Any],
    ) -> ToolRequest:
        normalized: dict[str, Any] = dict(request)
        args = dict(request.get("args") or {})
        normalized["args"] = args
        tool = tools.get(str(request.get("name", "") or ""))
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
    "SchemaArgsNormalizer",
    "ToolCallNormalizer",
    "as_normalizers",
    "tool_input_schema",
]