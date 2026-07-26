"""Executor graph nodes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from src.browser import BrowserProvider
from src.agent.state import AgentState, ToolRequest, ToolResult
from src.harness.tools import ToolLoader, ToolRegistry


def _stringify_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _normalize_request(
    request: ToolRequest,
    state: AgentState,
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


async def _invoke_tool(tool: Any, request: ToolRequest) -> Any:
    args = dict(request.get("args") or {})
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(args)
    if hasattr(tool, "invoke"):
        return tool.invoke(args)
    return tool(**args)


def create_executor_node(
    tools: Sequence[Any] | None = None,
    tool_loader: ToolLoader | None = None,
    tool_registry: ToolRegistry | None = None,
    browser_providers: Sequence[BrowserProvider] | None = None,
) -> Callable[[AgentState], Any]:
    """Create an async node that executes approved tool requests."""

    active_browser_providers = list(browser_providers or [])
    registry = tool_registry or ToolRegistry(
        providers=active_browser_providers or None,
        tools=None if active_browser_providers else tools,
        tool_loader=tool_loader,
    )
    if not active_browser_providers:
        active_browser_providers = registry.get_browser_providers()

    async def executor_node(state: AgentState) -> dict[str, Any]:
        request = state.get("tool_request") or {}
        tool_name = request.get("name", "")
        if not tool_name:
            result: ToolResult = {
                "name": "",
                "status": "error",
                "content": "",
                "error": "No tool request was provided.",
            }
            return {"tool_result": result, "error": result["error"]}

        normalized_request = _normalize_request(request, state, active_browser_providers)
        tool_name = normalized_request.get("name", "")
        tools_by_name = await registry.get()
        tool = tools_by_name.get(tool_name)
        if tool is None:
            available = ", ".join(sorted(tools_by_name)) or "none"
            result = {
                "name": tool_name,
                "status": "error",
                "content": "",
                "error": f"Unknown tool: {tool_name}. Available tools: {available}",
            }
            return {"tool_result": result, "error": result["error"]}

        try:
            value = await _invoke_tool(tool, normalized_request)
        except Exception as exc:  # noqa: BLE001 - tool failures must be state data
            raw_result: ToolResult = {
                "name": tool_name,
                "status": "error",
                "content": "",
                "error": str(exc),
            }
            result = _normalize_result(raw_result, active_browser_providers)
            return {"tool_result": result, "error": result["error"]}

        raw_result: ToolResult = {
            "name": tool_name,
            "status": "success",
            "content": _stringify_result(value),
            "error": "",
        }
        result = _normalize_result(raw_result, active_browser_providers)
        return {"tool_result": result, "error": result.get("error", "")}

    return executor_node
