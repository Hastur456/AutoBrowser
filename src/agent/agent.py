"""Top-level AutoBrowser agent graph assembly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph

from src.browser import BrowserProvider
from src.harness.memory import ensure_message_history
from src.agent.nodes import create_agent_node, human_input_node
from src.harness.policy import policy_node as default_policy_node
from src.agent.routers import (
    route_agent_decision,
    route_human_decision,
    route_policy_decision,
)
from src.agent.state import AgentState
from src.agent.subgraphs.executor.workflow import build_executor_graph
from src.agent.subgraphs.observer.workflow import build_observer_graph
from src.agent.subgraphs.planner.workflow import build_planner_graph
from src.harness.context import ContextBuilder
from src.harness.tools import ToolRegistry

DEFAULT_OLLAMA_MODEL = "gpt-oss:20b-cloud"


def create_default_llm() -> ChatOllama:
    """Create the default deterministic Ollama chat model."""

    return ChatOllama(model=DEFAULT_OLLAMA_MODEL, temperature=0)


def build_agent_graph(
    llm: Any | None = None,
    observer_llm: Any | None = None,
    tools: Sequence[Any] | None = None,
    tool_loader: Callable[[], Awaitable[Sequence[Any]]] | None = None,
    tool_registry: ToolRegistry | None = None,
    browser_providers: Sequence[BrowserProvider] | None = None,
    policy_node: Callable[[AgentState], dict[str, Any]] = default_policy_node,
    history_builder: Callable[[AgentState], list[BaseMessage]] = ensure_message_history,
    context_builder: ContextBuilder | None = None,
    checkpointer: Any | None = None,
    compress_tools: bool = False,
) -> Any:
    """Build and compile the AutoBrowser graph."""

    model = llm or create_default_llm()
    active_browser_providers = list(browser_providers or [])
    registry = tool_registry or ToolRegistry(
        providers=active_browser_providers or None,
        tools=tools,
        tool_loader=tool_loader,
    )
    graph = StateGraph(AgentState)

    graph.add_node("plan", build_planner_graph(model, history_builder=history_builder))
    graph.add_node(
        "agent",
        create_agent_node(
            model,
            tool_registry=registry,
            history_builder=history_builder,
            context_builder=context_builder,
        ),
    )
    graph.add_node("policy", policy_node)
    graph.add_node("human_input", human_input_node)
    graph.add_node(
        "executor",
        build_executor_graph(
            tools=tools,
            tool_loader=tool_loader,
            tool_registry=registry,
            browser_providers=active_browser_providers or None,
        ),
    )
    graph.add_node(
        "observe",
        build_observer_graph(
            observer_llm=(observer_llm or model) if compress_tools else None,
            compress_tools=compress_tools,
        ),
    )

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "agent")
    graph.add_conditional_edges(
        "agent",
        route_agent_decision,
        {"policy": "policy", "plan": "plan", "done": END, END: END},
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
        tool_registry: ToolRegistry | None = None,
        browser_providers: Sequence[BrowserProvider] | None = None,
        policy_node: Callable[[AgentState], dict[str, Any]] = default_policy_node,
        history_builder: Callable[[AgentState], list[BaseMessage]] = ensure_message_history,
        context_builder: ContextBuilder | None = None,
        checkpointer: Any | None = None,
        compress_tools: bool = False,
    ) -> None:
        self.graph = build_agent_graph(
            llm=llm,
            observer_llm=observer_llm,
            tools=tools,
            tool_loader=tool_loader,
            tool_registry=tool_registry,
            browser_providers=browser_providers,
            policy_node=policy_node,
            history_builder=history_builder,
            context_builder=context_builder,
            checkpointer=checkpointer,
            compress_tools=compress_tools,
        )
