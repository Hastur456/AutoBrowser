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

The broker is split into :meth:`ToolBroker.prepare` (normalize + resolve the tool and its
server) and :meth:`ToolBroker.invoke` (call + ``normalize_result``) so lifecycle hooks can
see the normalized request between the two; :meth:`ToolBroker.execute` composes them.

The broker takes the tool request and a plain provider-facing state mapping (from
:meth:`~src.agent_loop.execution.state.LoopState.snapshot_mapping`) and returns just the
:class:`ToolResult`; the loop folds it back into ``LoopState``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PreparedToolCall:
    """A tool request resolved against the registry, ready for :meth:`ToolBroker.invoke`.

    ``request`` is the request after every ``normalize_request``; ``tool`` is the resolved
    registry entry, or ``None`` when the name was empty or unknown — then ``error_result``
    holds the ready (not yet result-normalized) error. ``server`` is ``MCPTool.server`` and
    ``""`` for tools that are not MCP-backed. Lifecycle hooks see this resolved view.
    """

    request: ToolRequest
    tool: Any | None
    server: str
    error_result: ToolResult | None = None


class ToolBroker:
    """Execute one approved tool request against the registry and normalizers.

    :meth:`execute` is exactly ``invoke(await prepare(...))``; the loop calls the two halves
    separately when lifecycle hooks need to see (and possibly rewrite) the normalized request
    before it runs.
    """

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

    async def prepare(
        self,
        request: ToolRequest,
        state: Mapping[str, Any] | None = None,
    ) -> PreparedToolCall:
        """Normalize ``request`` and resolve its tool; never raises for a bad name."""

        provider_state: Mapping[str, Any] = state or {}
        if not request.get("name", ""):
            return PreparedToolCall(
                request=dict(request),
                tool=None,
                server="",
                error_result={
                    "name": "",
                    "status": "error",
                    "content": "",
                    "error": "No tool request was provided.",
                },
            )

        tools_by_name = await self._registry.get()
        normalized_request = _normalize_request(request, provider_state, tools_by_name, self._normalizers)
        tool_name = normalized_request.get("name", "")
        tool = tools_by_name.get(tool_name)
        if tool is None:
            return PreparedToolCall(
                request=normalized_request,
                tool=None,
                server="",
                error_result=_unknown_tool_result(tool_name, tools_by_name),
            )
        return PreparedToolCall(
            request=normalized_request,
            tool=tool,
            server=str(getattr(tool, "server", "") or ""),
        )

    async def invoke(self, prepared: PreparedToolCall) -> ToolResult:
        """Run a prepared call and return the normalized :class:`ToolResult`.

        A call without a tool only has its ready ``error_result`` normalized.
        """

        if prepared.tool is None:
            return _normalize_result(dict(prepared.error_result or {}), self._normalizers)

        tool_name = prepared.request.get("name", "")
        try:
            value = await _invoke_tool(prepared.tool, prepared.request)
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

    async def execute(
        self,
        request: ToolRequest,
        state: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Execute ``request`` and return the normalized :class:`ToolResult`."""

        return await self.invoke(await self.prepare(request, state))


__all__ = [
    "PreparedToolCall",
    "ToolBroker",
]