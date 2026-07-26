"""Executor graph nodes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from src.agent.state import AgentState, ToolRequest, ToolResult
from src.harness.tools import ToolLoader, ToolRegistry
from src.mcp.playwright_provider import PlaywrightMCPBrowserProvider


def _stringify_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


_playwright_provider = PlaywrightMCPBrowserProvider()


def _prepare_tool_args(tool: Any, request: ToolRequest, state: AgentState) -> dict[str, Any]:
    args = dict(request.get("args") or {})
    tool_name = str(request.get("name", ""))
    if _playwright_provider.supports(tool_name):
        return _playwright_provider.prepare_args(tool, request, state)
    return args


async def _invoke_tool(tool: Any, request: ToolRequest, state: AgentState) -> Any:
    args = _prepare_tool_args(tool, request, state)
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(args)
    if hasattr(tool, "invoke"):
        return tool.invoke(args)
    return tool(**args)


def create_executor_node(
    tools: Sequence[Any] | None = None,
    tool_loader: ToolLoader | None = None,
    tool_registry: ToolRegistry | None = None,
) -> Callable[[AgentState], Any]:
    """Create an async node that executes approved tool requests."""

    registry = tool_registry or ToolRegistry(tools=tools, tool_loader=tool_loader)

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
            value = await _invoke_tool(tool, request, state)
        except Exception as exc:  # noqa: BLE001 - tool failures must be state data
            result = {
                "name": tool_name,
                "status": "error",
                "content": "",
                "error": str(exc),
            }
            return {"tool_result": result, "error": result["error"]}

        result = {
            "name": tool_name,
            "status": "success",
            "content": _stringify_result(value),
            "error": "",
        }
        return {"tool_result": result, "error": ""}

    return executor_node
