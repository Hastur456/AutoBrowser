"""Executor subgraph assembly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agent.subgraphs.executor.nodes import create_executor_node
from src.agent.subgraphs.executor.state import ExecutorState
from src.harness.tools import ToolRegistry


def build_executor_graph(
    tools: Sequence[Any] | None = None,
    tool_loader: Callable[[], Awaitable[Sequence[Any]]] | None = None,
    tool_registry: ToolRegistry | None = None,
) -> Any:
    """Build a one-shot executor graph."""

    graph = StateGraph(ExecutorState)
    graph.add_node(
        "executor",
        create_executor_node(
            tools=tools,
            tool_loader=tool_loader,
            tool_registry=tool_registry,
        ),
    )
    graph.add_edge(START, "executor")
    graph.add_edge("executor", END)
    return graph.compile()
