"""Executor routers."""

from __future__ import annotations

from typing import Literal

from src.subgraphs.executor.state import ExecutorState


def route_executor_result(state: ExecutorState) -> Literal["success", "error"]:
    """Route based on the normalized tool result."""

    result = state.get("tool_result") or {}
    return "success" if result.get("status") == "success" else "error"
