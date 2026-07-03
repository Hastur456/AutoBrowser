"""Executor graph nodes."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from src.agent.state import AgentState, ToolRequest, ToolResult
from src.mcp.mcp_setup import setup_mcp

ToolLoader = Callable[[], Awaitable[Sequence[Any]]]


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def _stringify_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


class ToolRegistry:
    """Lazy registry for LangChain/MCP tools."""

    def __init__(
        self,
        tools: Sequence[Any] | None = None,
        tool_loader: ToolLoader | None = None,
    ) -> None:
        self._tools = list(tools) if tools is not None else None
        self._tool_loader = tool_loader or setup_mcp

    async def get(self) -> dict[str, Any]:
        if self._tools is None:
            self._tools = list(await self._tool_loader())
        return {_tool_name(tool): tool for tool in self._tools if _tool_name(tool)}


async def _invoke_tool(tool: Any, request: ToolRequest) -> Any:
    args = request.get("args") or {}
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(args)
    if hasattr(tool, "invoke"):
        return tool.invoke(args)
    return tool(**args)


def create_executor_node(
    tools: Sequence[Any] | None = None,
    tool_loader: ToolLoader | None = None,
) -> Callable[[AgentState], Any]:
    """Create an async node that executes approved tool requests."""

    registry = ToolRegistry(tools=tools, tool_loader=tool_loader)

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
            value = await _invoke_tool(tool, request)
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
