from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.tools import tool

from src.agent.agent import build_agent_graph
from src.agent.nodes import create_agent_node, observe_node
from src.agent.observe import MAX_CONTENT_PREVIEW_CHARS, MAX_EXECUTION_EVENTS
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


class RecordingLLM:
    def __init__(self) -> None:
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content='{"decision":"done","final_answer":"ok"}')


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
async def test_agent_node_uses_reasoning_context_in_prompt() -> None:
    llm = RecordingLLM()
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Inspect",
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "reasoning_context": "compact context",
            "observation": "raw observation",
            "history": ["raw history"],
        }
    )

    prompt = llm.messages[1].content
    assert result["final_answer"] == "ok"
    assert "compact context" in prompt
    assert "raw observation" not in prompt
    assert "raw history" not in prompt


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


def test_observe_node_compiles_structured_observation() -> None:
    result = observe_node(
        {
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": "page snapshot",
                "error": "",
            }
        }
    )

    assert result["latest_observation"]["outcome"] == "success"
    assert result["browser_context"]["page_summary"] == "page snapshot"
    assert result["recovery_signal"]["action"] == "none"
    assert result["execution_events"][0]["summary"] == (
        "browser_snapshot returned success: page snapshot"
    )
    assert result["observation"] == result["reasoning_context"]


def test_observe_node_compacts_large_tool_output() -> None:
    large_content = "x" * (MAX_CONTENT_PREVIEW_CHARS + 500)

    result = observe_node(
        {
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": large_content,
                "error": "",
            }
        }
    )

    preview = result["latest_observation"]["content_preview"]
    assert len(preview) <= MAX_CONTENT_PREVIEW_CHARS
    assert "truncated" in preview
    assert len(result["reasoning_context"]) < len(large_content)


def test_observe_node_classifies_repeated_tool_failures() -> None:
    first = observe_node(
        {
            "tool_result": {
                "name": "browser_click",
                "status": "error",
                "content": "",
                "error": "Element not found",
            }
        }
    )
    second = observe_node(
        {
            "execution_events": first["execution_events"],
            "tool_result": {
                "name": "browser_click",
                "status": "error",
                "content": "",
                "error": "Element not found",
            },
        }
    )

    assert first["recovery_signal"]["action"] == "retry"
    assert first["recovery_signal"]["repeat_count"] == 1
    assert second["recovery_signal"]["action"] == "replan"
    assert second["recovery_signal"]["repeat_count"] == 2


def test_observe_node_bounds_execution_events() -> None:
    state = {}
    for index in range(MAX_EXECUTION_EVENTS + 3):
        state = observe_node(
            {
                "execution_events": state.get("execution_events", []),
                "history": state.get("history", []),
                "tool_result": {
                    "name": "browser_snapshot",
                    "status": "success",
                    "content": f"snapshot {index}",
                    "error": "",
                },
            }
        )

    assert len(state["execution_events"]) == MAX_EXECUTION_EVENTS
    assert len(state["history"]) == MAX_EXECUTION_EVENTS
    assert state["execution_events"][0]["sequence"] == 4
    assert state["execution_events"][-1]["sequence"] == MAX_EXECUTION_EVENTS + 3


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
    assert result["latest_observation"]["outcome"] == "success"
    assert result["recovery_signal"]["action"] == "none"
