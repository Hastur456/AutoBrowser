from __future__ import annotations

import pytest
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from src.agent.agent import build_agent_graph
from src.agent.nodes import create_agent_node, create_observe_node, observe_node
from src.agent.observe import MAX_CONTENT_PREVIEW_CHARS
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


class RecordingObserverLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content=self.response)


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
            "task": "Navigate to Habr",
            "plan": [{"id": 1, "description": "Navigate to Habr", "status": "pending"}],
            "current_step": 0,
        }
    )

    assert llm.bound_tools == [browser_navigate]
    assert result["decision"] == "tool_call"
    assert result["tool_request"]["name"] == "browser_navigate"
    assert result["tool_request"]["args"] == {"url": "https://habr.com"}
    assert result["last_tool"] == "browser_navigate"
    assert result["repeat_count"] == 1


@pytest.mark.asyncio
async def test_agent_node_uses_observation_snapshot_and_refs_in_prompt() -> None:
    llm = RecordingLLM()
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Inspect",
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "observation": "Search box found.",
            "snapshot": '- textbox "Search" ref=e8',
            "refs": ["e8"],
        }
    )

    prompt = llm.messages[1].content
    assert result["final_answer"] == "ok"
    assert "Search box found." in prompt
    assert 'textbox "Search" ref=e8' in prompt
    assert "e8" in prompt


@pytest.mark.asyncio
async def test_agent_node_replans_on_third_identical_tool_request() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect again"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Inspect",
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "last_tool": "browser_snapshot",
            "last_args": {},
            "repeat_count": 2,
        }
    )

    assert result["decision"] == "replan"
    assert result["repeat_count"] == 3
    assert "three consecutive times" in result["observation"]
    assert "recovery_signal" not in result


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


def test_observe_node_translates_snapshot_and_advances_plan() -> None:
    snapshot = '- button "Search" ref=e7\n- textbox "Query" ref=e8'
    result = observe_node(
        {
            "plan": [
                {"id": 1, "description": "Inspect page", "status": "pending"},
                {"id": 2, "description": "Click search", "status": "pending"},
            ],
            "current_step": 0,
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": snapshot,
                "error": "",
            },
        }
    )

    assert result["snapshot"] == snapshot
    assert result["refs"] == ["e7", "e8"]
    assert "Refs:" in result["observation"]
    assert result["current_step"] == 1
    assert result["plan"][0]["status"] == "done"
    assert result["plan"][1]["status"] == "in_progress"
    assert "latest_observation" not in result
    assert "browser_context" not in result
    assert "recovery_signal" not in result
    assert "execution_events" not in result


def test_observe_node_replaces_snapshot_refs() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Old" ref=e1',
            "refs": ["e1"],
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- button "New" ref=e2',
                "error": "",
            },
        }
    )

    assert result["snapshot"] == '- button "New" ref=e2'
    assert result["refs"] == ["e2"]
    assert "e1" not in result["observation"]


def test_observe_node_clears_snapshot_after_browser_action() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Search" ref=e7',
            "refs": ["e7"],
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )

    assert result["snapshot"] == ""
    assert result["refs"] == []
    assert "Clicked" in result["observation"]


def test_observe_node_invalid_ref_is_plain_observation() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Old" ref=e119',
            "refs": ["e119"],
            "tool_result": {
                "name": "browser_click",
                "status": "error",
                "content": "",
                "error": "Ref e119 not found",
            },
        }
    )

    assert result["snapshot"] == ""
    assert result["refs"] == []
    assert result["error"] == "Ref e119 not found"
    assert "Tool failed." in result["observation"]
    assert "Ref not found" in result["observation"]
    assert "fresh browser_snapshot" in result["observation"]


def test_observe_node_preserves_large_browser_snapshot_but_compacts_observation() -> None:
    large_content = '- button "Save" ref=e42\n' + ("x" * (MAX_CONTENT_PREVIEW_CHARS + 500))

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

    assert result["snapshot"] == large_content
    assert result["refs"] == ["e42"]
    assert len(result["observation"]) < len(large_content)


@pytest.mark.asyncio
async def test_observe_node_llm_receives_only_tool_result() -> None:
    observer_llm = RecordingObserverLLM(
        (
            '{"summary":"Snapshot compressed","visible_state":"Search box visible",'
            '"important_refs":["e8"],"errors":[],"next_observation_hint":""}'
        )
    )
    node = create_observe_node(observer_llm)

    result = await node(
        {
            "task": "secret task",
            "plan": [{"id": 1, "description": "secret plan", "status": "pending"}],
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- textbox "Search" ref=e8',
                "error": "",
            },
        }
    )

    payload = observer_llm.messages[1].content
    assert "browser_snapshot" in payload
    assert "secret task" not in payload
    assert "secret plan" not in payload
    assert "Snapshot compressed" in result["observation"]
    assert result["refs"] == ["e8"]


@pytest.mark.asyncio
async def test_observe_node_invalid_llm_json_falls_back() -> None:
    observer_llm = RecordingObserverLLM("not json")
    node = create_observe_node(observer_llm)

    result = await node(
        {
            "tool_result": {
                "name": "browser_network_requests",
                "status": "success",
                "content": "GET https://example.test 200",
                "error": "",
            }
        }
    )

    assert "browser_network_requests returned success" in result["observation"]


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
            (
                '{"summary":"Snapshot compressed","visible_state":"snapshot text",'
                '"important_refs":[],"errors":[],"next_observation_hint":""}'
            ),
            '{"decision":"done","final_answer":"observed"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot])

    result = await graph.ainvoke({"task": "Inspect"})

    assert result["final_answer"] == "observed"
    assert "Snapshot compressed" in result["observation"]
    assert result["snapshot"] == "snapshot text"
    assert result["current_step"] == 1
    assert "recovery_signal" not in result
