"""Playwright MCP browser tool adaptation helpers."""

from __future__ import annotations

import re
from typing import Any

from src.agent.state import AgentState, ToolRequest


class PlaywrightMCPBrowserProvider:
    """Adapt browser tool arguments to the active Playwright MCP schema."""

    def supports(self, tool_name: str) -> bool:
        return str(tool_name).startswith("browser_")

    def prepare_args(self, tool: Any, request: ToolRequest, state: AgentState) -> dict[str, Any]:
        args = dict(request.get("args") or {})
        if not self.supports(str(request.get("name", ""))):
            return args

        properties = self._schema_properties(tool)
        if not properties:
            return args

        has_target = "target" in properties
        has_ref = "ref" in properties

        if has_target and "target" not in args and "ref" in args:
            args["target"] = args["ref"]
        if has_ref and "ref" not in args and self._looks_like_ref(args.get("target")):
            args["ref"] = args["target"]

        if "element" in properties and "element" not in args:
            ref = str(args.get("ref") or args.get("target") or "")
            if ref:
                args["element"] = self._element_description_from_snapshot(
                    str(state.get("snapshot", "") or ""),
                    ref,
                )

        if self._schema_additional_properties(tool) is False:
            args = {key: value for key, value in args.items() if key in properties}

        return args

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

    def _snapshot_line_for_ref(self, snapshot: str, ref: str) -> str:
        pattern = re.compile(rf"(?:\bref=|\[ref=){re.escape(ref)}(?:\b|\])")
        for line in snapshot.splitlines():
            if pattern.search(line):
                return line.strip()
        return ""

    def _element_description_from_snapshot(self, snapshot: str, ref: str) -> str:
        line = self._snapshot_line_for_ref(snapshot, ref)
        if not line:
            return ref

        text = re.sub(rf"\s*\[?ref={re.escape(ref)}\]?", "", line).strip()
        text = re.sub(r"^\s*[-*]\s*", "", text).strip()
        return text.rstrip(":") or ref

    def _looks_like_ref(self, value: Any) -> bool:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(value or "")))


_provider = PlaywrightMCPBrowserProvider()


def element_description_from_snapshot(snapshot: str, ref: str) -> str:
    return _provider._element_description_from_snapshot(snapshot, ref)


__all__ = ["PlaywrightMCPBrowserProvider", "element_description_from_snapshot"]
