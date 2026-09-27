"""Engine-native tool execution broker.

Ported from the legacy executor node. The dispatch behavior is preserved: empty-name
short-circuit, per-normalizer ``normalize_request`` folding, name-map lookup via
:meth:`ToolRegistry.get`, neutral ``invoke(args)`` dispatch, browser-aware unknown-tool
result, broad ``except`` -> ``status="error"``, and ``normalize_result`` folding.

Changes for the MCP Manager migration:

* ``browser_providers`` (``BrowserProvider``) are replaced by stateless
  :class:`~src.harness.normalization.ToolCallNormalizer` objects. They receive the current
  ``{name: tool}`` map instead of owning a tool list — tools come from the MCP Manager.
  Without explicit ``normalizers`` the broker uses ``ToolRegistry.get_normalizers()``
  (as it previously fell back to ``get_browser_providers()``).
* A failing tool whose exception carries ``error_code`` (all ``src.mcp.errors``, e.g.
  ``mcp_connection_lost`` / ``mcp_request_timeout``) keeps that code in the
  :class:`ToolResult`, so the loop can tell "tool failed" from "browser server restarted".

The broker takes the tool request and a plain provider-facing state mapping (from
:meth:`~src.agent_loop.execution.state.LoopState.snapshot_mapping`) and returns just the
:class:`ToolResult`; the loop folds it back into ``LoopState``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.contracts import ToolRequest, ToolResult
from src.harness.normalization import ToolCallNormalizer, as_normalizers
from src.harness.tools import ToolRegistry


def _stringify_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _normalize_request(
    request: ToolRequest,
    state: Mapping[str, Any],
    tools_by_name: Mapping[str, Any],
    normalizers: Sequence[ToolCallNormalizer],
) -> ToolRequest:
    normalized_request = dict(request)
    for normalizer in normalizers:
        normalized_request = normalizer.normalize_request(normalized_request, state, tools_by_name)
    return normalized_request


def _normalize_result(
    result: ToolResult,
    normalizers: Sequence[ToolCallNormalizer],
) -> ToolResult:
    normalized_result = dict(result)
    for normalizer in normalizers:
        normalized_result = normalizer.normalize_result(normalized_result)
    return normalized_result


def _unknown_tool_result(
    tool_name: str,
    tools_by_name: Mapping[str, Any],
) -> ToolResult:
    available = ", ".join(sorted(tools_by_name)) or "none"
    return {
        "name": tool_name,
        "status": "error",
        "content": "",
        "error": f"Unknown tool: {tool_name}. Available tools: {available}",
    }


async def _invoke_tool(tool: Any, request: ToolRequest) -> Any:
    args = dict(request.get("args") or {})
    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        result = invoke(args)
        if inspect.isawaitable(result):
            return await result
        return result
    if callable(tool):
        result = tool(**args)
        if inspect.isawaitable(result):
            return await result
        return result
    raise TypeError(f"Tool {tool!r} is not invocable.")


class ToolBroker:
    """Execute one approved tool request against the registry and normalizers."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        normalizers: ToolCallNormalizer | Sequence[ToolCallNormalizer] | None = None,
    ) -> None:
        self._registry = tool_registry
        active_normalizers = as_normalizers(normalizers)
        if not active_normalizers:
            get_normalizers = getattr(tool_registry, "get_normalizers", None)
            active_normalizers = as_normalizers(get_normalizers() if callable(get_normalizers) else None)
        self._normalizers: list[ToolCallNormalizer] = active_normalizers

    async def execute(
        self,
        request: ToolRequest,
        state: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Execute ``request`` and return the normalized :class:`ToolResult`."""

        provider_state: Mapping[str, Any] = state or {}
        tool_name = request.get("name", "")
        if not tool_name:
            return {
                "name": "",
                "status": "error",
                "content": "",
                "error": "No tool request was provided.",
            }

        tools_by_name = await self._registry.get()
        normalized_request = _normalize_request(request, provider_state, tools_by_name, self._normalizers)
        tool_name = normalized_request.get("name", "")
        tool = tools_by_name.get(tool_name)
        if tool is None:
            raw_result = _unknown_tool_result(tool_name, tools_by_name)
            return _normalize_result(raw_result, self._normalizers)

        try:
            value = await _invoke_tool(tool, normalized_request)
        except Exception as exc:  # noqa: BLE001 - tool failures must be state data
            raw_result: ToolResult = {
                "name": tool_name,
                "status": "error",
                "content": "",
                "error": str(exc),
            }
            error_code = str(getattr(exc, "error_code", "") or "")
            if error_code:
                raw_result["error_code"] = error_code
            return _normalize_result(raw_result, self._normalizers)

        raw_result = {
            "name": tool_name,
            "status": "success",
            "content": _stringify_result(value),
            "error": "",
        }
        return _normalize_result(raw_result, self._normalizers)


__all__ = [
    "ToolBroker",
]