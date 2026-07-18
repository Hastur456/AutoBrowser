"""Observer subgraph assembly."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agent.subgraphs.observer.nodes import create_observe_node
from src.agent.subgraphs.observer.state import ObserverState


def build_observer_graph(
    observer_llm: Any | None = None,
    *,
    compress_tools: bool = False,
) -> Any:
    """Build a one-shot observer graph."""

    graph = StateGraph(ObserverState)
    graph.add_node(
        "observe",
        create_observe_node(observer_llm, compress_tools=compress_tools),
    )
    graph.add_edge(START, "observe")
    graph.add_edge("observe", END)
    return graph.compile()
