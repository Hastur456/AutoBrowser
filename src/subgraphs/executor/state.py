"""Executor state contracts."""

from __future__ import annotations

from typing import TypedDict

from src.agent.state import ToolRequest, ToolResult


class ExecutorState(TypedDict, total=False):
    """State accepted and returned by the executor subgraph."""

    tool_request: ToolRequest
    tool_result: ToolResult
    error: str
