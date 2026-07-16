"""Executor graph nodes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from src.agent.state import AgentState, ToolRequest, ToolResult
from src.harness.tools import ToolLoader, ToolRegistry


def _stringify_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _schema_dict(tool: Any) -> dict[str, Any]:
    for attr in ("args_schema", "input_schema"):
        schema = getattr(tool, attr, None)
        if schema is None:
            continue
        if isinstance(schema, dict):
            return schema
        if hasattr(schema, "model_json_schema"):
            return schema.model_json_schema()
        if hasattr(schema, "schema"):
            return schema.schema()

    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        if "properties" in args:
            return args
        return {"properties": args}

    return {}


def _schema_properties(tool: Any) -> dict[str, Any]:
    properties = _schema_dict(tool).get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _schema_additional_properties(tool: Any) -> Any:
    return _schema_dict(tool).get("additionalProperties")


def _snapshot_line_for_ref(snapshot: str, ref: str) -> str:
    pattern = re.compile(rf"(?:\bref=|\[ref=){re.escape(ref)}(?:\b|\])")
    for line in snapshot.splitlines():
        if pattern.search(line):
            return line.strip()
    return ""


def _element_description_from_snapshot(snapshot: str, ref: str) -> str:
    line = _snapshot_line_for_ref(snapshot, ref)
    if not line:
        return ref

    text = re.sub(rf"\s*\[?ref={re.escape(ref)}\]?", "", line).strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    return text.rstrip(":") or ref


def _looks_like_ref(value: Any) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(value or "")))


def _normalize_tool_args(tool: Any, request: ToolRequest, state: AgentState) -> dict[str, Any]:
    """Adapt browser ref arguments to the loaded Playwright MCP tool schema."""

    args = dict(request.get("args") or {})
    if not str(request.get("name", "")).startswith("browser_"):
        return args

    properties = _schema_properties(tool)
    if not properties:
        return args

    has_target = "target" in properties
    has_ref = "ref" in properties

    if has_target and "target" not in args and "ref" in args:
        args["target"] = args["ref"]
    if has_ref and "ref" not in args and _looks_like_ref(args.get("target")):
        args["ref"] = args["target"]

    if "element" in properties and "element" not in args:
        ref = str(args.get("ref") or args.get("target") or "")
        if ref:
            args["element"] = _element_description_from_snapshot(
                str(state.get("snapshot", "") or ""),
                ref,
            )

    if _schema_additional_properties(tool) is False:
        args = {key: value for key, value in args.items() if key in properties}

    return args


async def _invoke_tool(tool: Any, request: ToolRequest, state: AgentState) -> Any:
    args = _normalize_tool_args(tool, request, state)
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
