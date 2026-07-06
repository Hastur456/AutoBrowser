from __future__ import annotations

import pytest
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from src.agent.agent import build_agent_graph
from src.agent.nodes import create_agent_node, create_observe_node, observe_node
from src.agent.observe import MAX_CONTENT_PREVIEW_CHARS, compile_observation
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

    prompt = llm.messages[-1].content
    assert result["final_answer"] == "ok"
    assert "Original user request:\nInspect" in llm.messages[1].content
    assert "Search box found." in prompt
    assert 'textbox "Search" ref=e8' in prompt
    assert "e8" in prompt
    assert all('textbox "Search" ref=e8' not in message.content for message in result["messages"])


@pytest.mark.asyncio
async def test_agent_node_deepens_snapshot_on_third_identical_snapshot_request() -> None:
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

    assert result["decision"] == "tool_call"
    assert result["tool_request"]["name"] == "browser_snapshot"
    assert result["tool_request"]["args"] == {"depth": 4}
    assert result["snapshot_recovery_count"] == 1
    assert result["repeat_count"] == 1
    assert result["messages"][-1].type == "ai"
    assert result["messages"][-1].tool_calls[0]["args"] == {"depth": 4}


@pytest.mark.asyncio
async def test_agent_node_replans_after_snapshot_recovery_is_exhausted() -> None:
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
            "snapshot_recovery_count": 1,
        }
    )

    assert result["decision"] == "replan"
    assert result["repeat_count"] == 3
    assert "three consecutive times" in result["observation"]
    assert result["messages"][-1].type == "tool"
    assert "three consecutive times" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_agent_node_replans_on_third_identical_non_snapshot_tool_request() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"ref":"e1"},"reason":"Click again"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Click",
            "plan": [{"id": 1, "description": "Click button", "status": "pending"}],
            "current_step": 0,
            "last_tool": "browser_click",
            "last_args": {"ref": "e1"},
            "repeat_count": 2,
        }
    )

    assert result["decision"] == "replan"
    assert result["repeat_count"] == 3
    assert "three consecutive times" in result["observation"]


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


def test_observe_node_translates_snapshot_without_advancing_plan_on_tool_success() -> None:
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
    assert "current_step" not in result
    assert "plan" not in result
    assert "latest_observation" not in result
    assert "browser_context" not in result
    assert "recovery_signal" not in result
    assert "execution_events" not in result


def test_observe_node_advances_plan_only_with_step_completion_evidence() -> None:
    result = compile_observation(
        {
            "plan": [
                {"id": 1, "description": "Locate search input", "status": "pending"},
                {"id": 2, "description": "Type query", "status": "pending"},
            ],
            "current_step": 0,
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- textbox "Search" ref=e8',
                "error": "",
            },
        },
        {
            "summary": "Search input found.",
            "visible_state": "Search input is visible.",
            "important_refs": ["e8"],
            "errors": [],
            "next_observation_hint": "",
        },
    )

    assert result["current_step"] == 1
    assert result["plan"][0]["status"] == "done"
    assert result["plan"][1]["status"] == "in_progress"


def test_observe_node_does_not_advance_plan_for_unrelated_successful_actions() -> None:
    for tool_name, content, description in [
        ("browser_snapshot", '- textbox "Search" ref=e8', "Locate search input"),
        ("browser_click", "Clicked.", "Search products"),
        ("browser_navigate", "Navigated.", "Inspect results"),
    ]:
        result = compile_observation(
            {
                "plan": [{"id": 1, "description": description, "status": "pending"}],
                "current_step": 0,
                "tool_result": {
                    "name": tool_name,
                    "status": "success",
                    "content": content,
                    "error": "",
                },
            }
        )

        assert "current_step" not in result
        assert "plan" not in result


def test_observe_node_does_not_advance_plan_on_negative_evidence() -> None:
    result = compile_observation(
        {
            "plan": [{"id": 1, "description": "Locate search input", "status": "pending"}],
            "current_step": 0,
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- text "No search form" ref=e8',
                "error": "",
            },
        },
        {
            "summary": "Search input not found.",
            "visible_state": "Search input is not visible.",
            "important_refs": [],
            "errors": [],
            "next_observation_hint": "A different page may show the search input.",
        },
    )

    assert "current_step" not in result
    assert "plan" not in result


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
async def test_observe_node_appends_compact_tool_message_without_raw_snapshot() -> None:
    observer_llm = RecordingObserverLLM(
        (
            '{"summary":"Search input found.","visible_state":"Search input visible",'
            '"important_refs":["e8"],"errors":[],"next_observation_hint":""}'
        )
    )
    node = create_observe_node(observer_llm)
    raw_snapshot = '- textbox "SECRET RAW SNAPSHOT" ref=e8'

    result = await node(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "browser_snapshot", "args": {}, "id": "call_1"}
                    ],
                )
            ],
            "tool_request": {
                "name": "browser_snapshot",
                "args": {},
                "id": "call_1",
            },
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": raw_snapshot,
                "error": "",
            },
        }
    )

    tool_message = result["messages"][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.name == "browser_snapshot"
    assert "Search input found." in tool_message.content
    assert "Refs:" in tool_message.content
    assert "e8" in tool_message.content
    assert "SECRET RAW SNAPSHOT" not in tool_message.content


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
async def test_graph_stops_after_replan_limit() -> None:
    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            '{"decision":"replan","reason":"Need a different plan."}',
            '{"steps":[{"id":1,"description":"Inspect page again","status":"pending"}]}',
            '{"decision":"replan","reason":"Still not enough context."}',
            '{"steps":[{"id":1,"description":"Inspect page again","status":"pending"}]}',
            '{"decision":"replan","reason":"Still stuck."}',
            '{"steps":[{"id":1,"description":"Inspect page again","status":"pending"}]}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[])

    result = await graph.ainvoke({"task": "Inspect"}, {"recursion_limit": 20})

    assert result["decision"] == "done"
    assert result["replan_count"] == 3
    assert "Blocked: replanning reached the limit of 3" in result["final_answer"]


@pytest.mark.asyncio
async def test_graph_stops_after_consecutive_tool_failures() -> None:
    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        raise ConnectionError("connect ECONNREFUSED ::1:9222")

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect page"}}'
            ),
            '{"summary":"failed","visible_state":"","important_refs":[],"errors":["down"],"next_observation_hint":""}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{"depth":4},"reason":"Inspect deeper"}}'
            ),
            '{"summary":"failed","visible_state":"","important_refs":[],"errors":["down"],"next_observation_hint":""}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{"depth":6},"reason":"Inspect deeper"}}'
            ),
            '{"summary":"failed","visible_state":"","important_refs":[],"errors":["down"],"next_observation_hint":""}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot])

    result = await graph.ainvoke({"task": "Inspect"}, {"recursion_limit": 20})

    assert result["decision"] == "done"
    assert result["consecutive_failures"] == 3
    assert "Blocked: tool execution failed 3 consecutive times" in result["final_answer"]
    assert "connect ECONNREFUSED ::1:9222" in result["final_answer"]


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
    assert result.get("current_step", 0) == 0
    assert result["messages"][0].type == "system"
    assert "Original user request:\nInspect" in result["messages"][1].content
    assert result["messages"][2].tool_calls[0]["name"] == "browser_snapshot"
    assert result["messages"][3].type == "tool"
    assert "Snapshot compressed" in result["messages"][3].content
    assert result["messages"][-1].content == "observed"
    assert "recovery_signal" not in result
