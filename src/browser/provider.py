"""Provider protocol for browser backends."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from src.agent.state import AgentState, ToolRequest, ToolResult


@runtime_checkable
class BrowserProvider(Protocol):
    """Protocol implemented by neutral browser backend adapters."""

    async def get_tools(self) -> Sequence[Any]:
        """Return the tools exposed by the provider."""

    def normalize_request(self, request: ToolRequest, state: AgentState) -> ToolRequest:
        """Normalize a graph tool request before tool execution."""

    def normalize_result(self, result: ToolResult) -> ToolResult:
        """Normalize a raw tool result before it re-enters the graph."""


__all__ = ["BrowserProvider"]
