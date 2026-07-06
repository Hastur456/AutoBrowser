"""Top-level AutoBrowser agent graph assembly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph

from src.agent.nodes import create_agent_node, create_observe_node, human_input_node
from src.agent.policy import policy_node
from src.agent.routers import (
    route_agent_decision,
    route_human_decision,
    route_policy_decision,
)
from src.agent.state import AgentState
from src.mcp.mcp_setup import setup_mcp
from src.subgraphs.executor.nodes import create_executor_node
from src.subgraphs.planner.nodes import create_plan_node

DEFAULT_OLLAMA_MODEL = "gpt-oss:20b-cloud"


def create_default_llm() -> ChatOllama:
    """Create the default deterministic Ollama chat model."""

    return ChatOllama(model=DEFAULT_OLLAMA_MODEL, temperature=0)


def build_agent_graph(
    llm: Any | None = None,
    observer_llm: Any | None = None,
    tools: Sequence[Any] | None = None,
    tool_loader: Callable[[], Awaitable[Sequence[Any]]] | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Build and compile the AutoBrowser graph."""

    model = llm or create_default_llm()
    graph = StateGraph(AgentState)

    graph.add_node("plan", create_plan_node(model))
    graph.add_node("agent", create_agent_node(model, tools=tools))
    graph.add_node("policy", policy_node)
    graph.add_node("human_input", human_input_node)
    graph.add_node(
        "executor",
        create_executor_node(tools=tools, tool_loader=tool_loader or setup_mcp),
    )
    graph.add_node("observe", create_observe_node(observer_llm or model))

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "agent")
    graph.add_conditional_edges(
        "agent",
        route_agent_decision,
        {"policy": "policy", "plan": "plan", END: END},
    )
    graph.add_conditional_edges(
        "policy",
        route_policy_decision,
        {"executor": "executor", "human_input": "human_input", "agent": "agent"},
    )
    graph.add_conditional_edges(
        "human_input",
        route_human_decision,
        {"executor": "executor", "agent": "agent"},
    )
    graph.add_edge("executor", "observe")
    graph.add_edge("observe", "agent")

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()


class AgentWorkflow:
    """Small compatibility wrapper exposing the compiled graph as ``.graph``."""

    def __init__(
        self,
        llm: Any | None = None,
        observer_llm: Any | None = None,
        tools: Sequence[Any] | None = None,
        tool_loader: Callable[[], Awaitable[Sequence[Any]]] | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.graph = build_agent_graph(
            llm=llm,
            observer_llm=observer_llm,
            tools=tools,
            tool_loader=tool_loader,
            checkpointer=checkpointer,
        )
