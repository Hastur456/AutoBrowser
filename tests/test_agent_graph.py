from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.tools import tool

from src.agent.agent import build_agent_graph
from src.agent.nodes import create_agent_node
from src.agent.policy import classify_tool_request
from src.agent.routers import (
    route_agent_decision,
    route_human_decision,
    route_policy_decision,
)
from src.subgraphs.executor.nodes import create_executor_node


def test_agent_routes() -> None:
    assert route_agent_decision({"decision": "tool_call"}) == "policy"
    assert route_agent_decision({"decision": "replan"}) == "plan"
    assert route_agent_decision({"decision": "done"}) == "__end__"


def test_policy_routes() -> None:
    assert route_policy_decision({"policy_decision": "approved"}) == "executor"
    assert route_policy_decision({"policy_decision": "needs_human"}) == "human_input"
    assert route_policy_decision({"policy_decision": "blocked"}) == "agent"
    assert route_human_decision({"policy_decision": "approved"}) == "executor"
    assert route_human_decision({"policy_decision": "blocked"}) == "agent"


def test_policy_classification() -> None:
    assert classify_tool_request({"name": "browser_snapshot", "args": {}})[0] == "approved"
    assert classify_tool_request({"name": "browser_click", "args": {}})[0] == "approved"
    assert classify_tool_request({"name": "browser_navigate", "args": {}})[0] == "approved"
    assert classify_tool_request({"name": "payment_submit", "args": {}})[0] == "blocked"


class ToolCallingFakeLLM:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools

        async def invoke(_messages):
            return self.response

        return RunnableLambda(invoke)


@pytest.mark.asyncio
async def test_agent_node_uses_bound_tool_calls() -> None:
    @tool
    def browser_navigate(url: str) -> str:
        """Navigate browser to a URL."""

        return url

    llm = ToolCallingFakeLLM(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "browser_navigate",
                    "args": {"url": "https://habr.com"},
                    "id": "call_1",
                }
            ],
        )
    )
    node = create_agent_node(llm, tools=[browser_navigate])

    result = await node(
        {
            "task": "Перейди на habr",
            "plan": [{"id": 1, "description": "Navigate to Habr", "status": "pending"}],
            "current_step": 0,
        }
    )

    assert llm.bound_tools == [browser_navigate]
    assert result["decision"] == "tool_call"
    assert result["tool_request"]["name"] == "browser_navigate"
    assert result["tool_request"]["args"] == {"url": "https://habr.com"}


@pytest.mark.asyncio
async def test_executor_success() -> None:
    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return "page snapshot"

    node = create_executor_node(tools=[browser_snapshot])
    result = await node({"tool_request": {"name": "browser_snapshot", "args": {}}})

    assert result["tool_result"]["status"] == "success"
    assert result["tool_result"]["content"] == "page snapshot"


@pytest.mark.asyncio
async def test_executor_unknown_tool() -> None:
    node = create_executor_node(tools=[])
    result = await node({"tool_request": {"name": "missing_tool", "args": {}}})

    assert result["tool_result"]["status"] == "error"
    assert "Unknown tool" in result["tool_result"]["error"]
    assert "Available tools: none" in result["tool_result"]["error"]


@pytest.mark.asyncio
async def test_graph_done_path() -> None:
    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Answer directly","status":"pending"}]}',
            '{"decision":"done","final_answer":"finished"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[])

    result = await graph.ainvoke({"task": "Finish"})

    assert result["final_answer"] == "finished"


@pytest.mark.asyncio
async def test_graph_tool_path() -> None:
    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return "snapshot text"

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect page"}}'
            ),
            '{"decision":"done","final_answer":"observed"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot])

    result = await graph.ainvoke({"task": "Inspect"})

    assert result["final_answer"] == "observed"
    assert "browser_snapshot returned success" in result["observation"]
