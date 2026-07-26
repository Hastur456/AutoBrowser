"""Fake browser backend for tests and deterministic replay."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import tool

from src.agent.state import AgentState, ToolRequest, ToolResult
from src.browser.errors import BROWSER_ERROR_ACTION_FAILED, BROWSER_ERROR_INVALID_REF
from src.browser.names import is_browser_tool_name, to_playwright_browser_name
from src.browser.provider import BrowserProvider

INVALID_REF_PATTERN = re.compile(
    r"\bRef\s+[A-Za-z][A-Za-z0-9_-]*\s+not\s+found\b",
    re.IGNORECASE,
)
REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
REF_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _has_invalid_ref_error(result: ToolResult) -> bool:
    payload = str(result.get("error", "") or result.get("content", "") or "")
    return bool(INVALID_REF_PATTERN.search(payload))


class FakeBrowserProvider(BrowserProvider):
    """Replay browser tools from a deterministic sequence of snapshots."""

    def __init__(self, snapshots: list[str]) -> None:
        if not snapshots:
            raise ValueError("FakeBrowserProvider requires at least one snapshot.")

        self._snapshots = list(snapshots)
        self._snapshot_index = 0
        self._tools = self._build_tools()

    async def get_tools(self) -> Sequence[Any]:
        return list(self._tools)

    def normalize_request(self, request: ToolRequest, state: AgentState) -> ToolRequest:
        normalized_request = dict(request)
        args = dict(request.get("args") or {})
        requested_name = str(request.get("name", "") or "").strip()
        if not is_browser_tool_name(requested_name):
            normalized_request["args"] = args
            return normalized_request

        tool_name = to_playwright_browser_name(requested_name)
        normalized_request["name"] = tool_name

        if tool_name in {"browser_click", "browser_hover", "browser_type"}:
            ref = self._ref_from_args(args)
            if ref:
                args.setdefault("ref", ref)
                args.setdefault("target", ref)

        normalized_request["args"] = args
        return normalized_request

    def normalize_result(self, result: ToolResult) -> ToolResult:
        normalized_result = dict(result)
        tool_name = str(normalized_result.get("name", "") or "")
        if (
            normalized_result.get("status") != "error"
            or not is_browser_tool_name(tool_name)
            or normalized_result.get("error_code")
        ):
            return normalized_result

        if _has_invalid_ref_error(normalized_result):
            normalized_result["error_code"] = BROWSER_ERROR_INVALID_REF
        else:
            normalized_result["error_code"] = BROWSER_ERROR_ACTION_FAILED
        return normalized_result

    def _build_tools(self) -> list[Any]:
        @tool("browser_navigate")
        async def browser_navigate(url: str) -> str:
            """Navigate to a URL in the fake browser."""

            self._advance_snapshot()
            return f"Navigated to {url}."

        @tool("browser_snapshot")
        async def browser_snapshot(depth: int | None = None) -> str:
            """Return the current fake browser snapshot."""

            _ = depth
            return self._current_snapshot()

        @tool("browser_click")
        async def browser_click(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Click an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Clicked ref {resolved_ref}."

        @tool("browser_type")
        async def browser_type(
            text: str,
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Type text into an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Typed into ref {resolved_ref}: {text}"

        @tool("browser_hover")
        async def browser_hover(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Hover an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Hovered ref {resolved_ref}."

        @tool("browser_evaluate")
        async def browser_evaluate(
            expression: str | None = None,
            script: str | None = None,
        ) -> dict[str, str]:
            """Evaluate a script in the fake browser without mutating page state."""

            payload = str(expression or script or "").strip()
            if not payload:
                raise ValueError(
                    "Fake browser evaluate requires an expression or script."
                )

            return {
                "source": "expression" if expression else "script",
                "expression": payload,
                "snapshot": self._current_snapshot(),
            }

        return [
            browser_navigate,
            browser_snapshot,
            browser_click,
            browser_type,
            browser_hover,
            browser_evaluate,
        ]

    def _current_snapshot(self) -> str:
        return self._snapshots[self._snapshot_index]

    def _advance_snapshot(self) -> None:
        if self._snapshot_index < len(self._snapshots) - 1:
            self._snapshot_index += 1

    def _assert_ref_exists(self, ref: str) -> None:
        if ref not in self._snapshot_refs(self._current_snapshot()):
            raise ValueError(f"Ref {ref} not found")

    def _require_ref(self, *, ref: str | None, target: str | None) -> str:
        resolved_ref = str(ref or "").strip()
        if resolved_ref:
            return resolved_ref

        resolved_target = str(target or "").strip()
        if self._looks_like_ref(resolved_target):
            return resolved_target

        raise ValueError("Fake browser action requires a ref or ref-like target.")

    def _ref_from_args(self, args: dict[str, Any]) -> str:
        ref = str(args.get("ref", "") or "").strip()
        if ref:
            return ref

        target = str(args.get("target", "") or "").strip()
        return target if self._looks_like_ref(target) else ""

    def _snapshot_refs(self, snapshot: str) -> set[str]:
        return {match.group(1) for match in REF_PATTERN.finditer(snapshot)}

    def _looks_like_ref(self, value: str) -> bool:
        return bool(REF_VALUE_PATTERN.fullmatch(value))


__all__ = ["FakeBrowserProvider"]
