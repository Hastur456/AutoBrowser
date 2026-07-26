"""Playwright MCP implementation of the neutral browser provider contract."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from src.agent.state import AgentState, ToolRequest, ToolResult
from src.browser.provider import BrowserProvider


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def _snapshot_line_for_ref(snapshot: str, ref: str) -> str:
    pattern = re.compile(rf"(?:\bref=|\[ref=){re.escape(ref)}(?:\b|\])")
    for line in snapshot.splitlines():
        if pattern.search(line):
            return line.strip()
    return ""


def element_description_from_snapshot(snapshot: str, ref: str) -> str:
    line = _snapshot_line_for_ref(snapshot, ref)
    if not line:
        return ref

    text = re.sub(rf"\s*\[?ref={re.escape(ref)}\]?", "", line).strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    return text.rstrip(":") or ref


class PlaywrightMCPBrowserProvider(BrowserProvider):
    """Adapt Playwright MCP tools to the neutral browser-provider protocol."""

    def __init__(self, tools: Sequence[Any] | None = None) -> None:
        self._tools = list(tools or [])

    async def get_tools(self) -> Sequence[Any]:
        return list(self._tools)

    def normalize_request(self, request: ToolRequest, state: AgentState) -> ToolRequest:
        normalized_request = dict(request)
        args = dict(request.get("args") or {})
        tool_name = str(request.get("name", ""))
        if not self._supports(tool_name):
            normalized_request["args"] = args
            return normalized_request

        tool = self._tools_by_name().get(tool_name)
        if tool is None:
            normalized_request["args"] = args
            return normalized_request

        properties = self._schema_properties(tool)
        if not properties:
            normalized_request["args"] = args
            return normalized_request

        has_target = "target" in properties
        has_ref = "ref" in properties

        if has_target and "target" not in args and "ref" in args:
            args["target"] = args["ref"]
        if has_ref and "ref" not in args and self._looks_like_ref(args.get("target")):
            args["ref"] = args["target"]

        if "element" in properties and "element" not in args:
            ref = str(args.get("ref") or args.get("target") or "")
            if ref:
                args["element"] = element_description_from_snapshot(
                    str(state.get("snapshot", "") or ""),
                    ref,
                )

        if self._schema_additional_properties(tool) is False:
            args = {key: value for key, value in args.items() if key in properties}

        normalized_request["args"] = args
        return normalized_request

    def normalize_result(self, result: ToolResult) -> ToolResult:
        return dict(result)

    def _tools_by_name(self) -> dict[str, Any]:
        return {_tool_name(tool): tool for tool in self._tools if _tool_name(tool)}

    def _supports(self, tool_name: str) -> bool:
        return str(tool_name).startswith("browser_")

    def _schema_dict(self, tool: Any) -> dict[str, Any]:
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

    def _schema_properties(self, tool: Any) -> dict[str, Any]:
        properties = self._schema_dict(tool).get("properties", {})
        return properties if isinstance(properties, dict) else {}

    def _schema_additional_properties(self, tool: Any) -> Any:
        return self._schema_dict(tool).get("additionalProperties")

    def _looks_like_ref(self, value: Any) -> bool:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(value or "")))


__all__ = ["PlaywrightMCPBrowserProvider", "element_description_from_snapshot"]
