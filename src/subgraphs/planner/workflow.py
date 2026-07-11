"""Planner subgraph assembly."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.subgraphs.planner.nodes import create_plan_node
from src.subgraphs.planner.state import PlannerState


def build_planner_graph(llm: Any) -> Any:
    """Build a one-shot planner graph."""

    graph = StateGraph(PlannerState)
    graph.add_node("plan", create_plan_node(llm))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", END)
    return graph.compile()
