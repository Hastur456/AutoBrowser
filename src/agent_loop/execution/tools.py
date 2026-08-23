"""Engine-native tool execution broker.

Ported from ``src/agent/subgraphs/executor/nodes.py`` (``executor_node`` and its
normalize/invoke/unknown-tool helpers). The dispatch behavior is preserved verbatim:
empty-name short-circuit, per-provider ``normalize_request`` folding, name-map lookup
via :meth:`ToolRegistry.get`, positional ``ainvoke(args)``, browser-aware unknown-tool
result, broad ``except`` → ``status="error"``, and ``normalize_result`` folding.

Unlike the graph node — which read/wrote ``AgentState`` — :class:`ToolBroker` takes the
tool request and a plain provider-facing state mapping (produced by
:meth:`~src.agent_loop.execution.state.LoopState.snapshot_mapping`) and returns just the
:class:`ToolResult`. The loop is responsible for folding that result back into
``LoopState`` before observation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.contracts import ToolRequest, ToolResult
from src.browser import (
    BROWSER_ERROR_UNKNOWN_ACTION,
    BrowserProvider,
    is_browser_tool_name,
    to_canonical_browser_name,
)
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
    browser_providers: Sequence[BrowserProvider],
) -> ToolRequest:
    normalized_request = dict(request)
    for provider in browser_providers:
        normalized_request = provider.normalize_request(normalized_request, state)
    return normalized_request


def _normalize_result(
    result: ToolResult,
    browser_providers: Sequence[BrowserProvider],
) -> ToolResult:
    normalized_result = dict(result)
    for provider in browser_providers:
        normalized_result = provider.normalize_result(normalized_result)
    return normalized_result


def _unknown_tool_result(
    tool_name: str,
    tools_by_name: dict[str, Any],
) -> ToolResult:
    if is_browser_tool_name(tool_name):
        available_browser_actions = ", ".join(
            sorted(
                {
                    to_canonical_browser_name(available_name)
                    for available_name in tools_by_name
                    if is_browser_tool_name(available_name)
                }
            )
        ) or "none"
        display_name = to_canonical_browser_name(tool_name)
        return {
            "name": tool_name,
            "status": "error",
            "content": "",
            "error": (
                f"Unknown browser action: {display_name}. "
                f"Available browser actions: {available_browser_actions}"
            ),
            "error_code": BROWSER_ERROR_UNKNOWN_ACTION,
        }

    available = ", ".join(sorted(tools_by_name)) or "none"
    return {
        "name": tool_name,
        "status": "error",
        "content": "",
        "error": f"Unknown tool: {tool_name}. Available tools: {available}",
    }


async def _invoke_tool(tool: Any, request: ToolRequest) -> Any:
    args = dict(request.get("args") or {})
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(args)
    if hasattr(tool, "invoke"):
        return tool.invoke(args)
    return tool(**args)


class ToolBroker:
    """Execute one approved tool request against the registry and browser providers."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        browser_providers: Sequence[BrowserProvider] | None = None,
    ) -> None:
        self._registry = tool_registry
        active_browser_providers = list(browser_providers or [])
        if not active_browser_providers:
            active_browser_providers = tool_registry.get_browser_providers()
        self._browser_providers = active_browser_providers

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

        normalized_request = _normalize_request(request, provider_state, self._browser_providers)
        tool_name = normalized_request.get("name", "")
        tools_by_name = await self._registry.get()
        tool = tools_by_name.get(tool_name)
        if tool is None:
            raw_result = _unknown_tool_result(tool_name, tools_by_name)
            return _normalize_result(raw_result, self._browser_providers)

        try:
            value = await _invoke_tool(tool, normalized_request)
        except Exception as exc:  # noqa: BLE001 - tool failures must be state data
            raw_result: ToolResult = {
                "name": tool_name,
                "status": "error",
                "content": "",
                "error": str(exc),
            }
            return _normalize_result(raw_result, self._browser_providers)

        raw_result = {
            "name": tool_name,
            "status": "success",
            "content": _stringify_result(value),
            "error": "",
        }
        return _normalize_result(raw_result, self._browser_providers)


__all__ = [
    "ToolBroker",
]
