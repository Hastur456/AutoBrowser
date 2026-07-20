"""Planner subgraph assembly."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph

from src.agent.state import AgentState
from src.harness.memory import ensure_message_history
from src.agent.subgraphs.planner.nodes import create_plan_node
from src.agent.subgraphs.planner.state import PlannerState


def build_planner_graph(
    llm: Any,
    *,
    history_builder: Callable[[AgentState], list[BaseMessage]] = ensure_message_history,
) -> Any:
    """Build a one-shot planner graph."""

    graph = StateGraph(PlannerState)
    graph.add_node("plan", create_plan_node(llm, history_builder=history_builder))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", END)
    return graph.compile()
