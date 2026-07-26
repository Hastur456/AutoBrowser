from __future__ import annotations

import pytest
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from src.agent.agent import build_agent_graph
from src.agent.nodes import create_agent_node, create_observe_node, observe_node
from src.browser.adapters import PlaywrightMCPBrowserProvider
from src.agent.subgraphs.observer.utils import MAX_CONTENT_PREVIEW_CHARS
from src.agent.subgraphs.observer.utils import extract_element_refs
from src.agent.subgraphs.observer.nodes import compile_observation
from src.agent.subgraphs.observer.workflow import build_observer_graph
from src.harness.policy import classify_tool_request, policy_node
from src.harness.tools import ToolRegistry
from src.agent.routers import (
    route_agent_decision,
    route_human_decision,
    route_policy_decision,
)
from src.agent.subgraphs.executor.nodes import create_executor_node


def test_agent_routes() -> None:
    assert route_agent_decision({"decision": "tool_call"}) == "policy"
    assert route_agent_decision({"decision": "replan"}) == "plan"
    assert route_agent_decision({"decision": "done"}) == "done"
    assert route_agent_decision({"decision": ""}) == "__end__"


def test_policy_routes() -> None:
    assert route_policy_decision({"policy_decision": "approved"}) == "executor"
    assert route_policy_decision({"policy_decision": "needs_human"}) == "human_input"
    assert route_policy_decision({"policy_decision": "blocked"}) == "agent"
    assert route_human_decision({"policy_decision": "approved"}) == "executor"
    assert route_human_decision({"policy_decision": "blocked"}) == "agent"


def test_policy_classification() -> None:
    assert (
        classify_tool_request(
            {"needs_fresh_snapshot": True},
            {"name": "browser_snapshot", "args": {}},
        )[0]
        == "approved"
    )
    assert (
        classify_tool_request(
            {"snapshot": '- button "Search" ref=e7', "needs_fresh_snapshot": False},
            {"name": "browser_snapshot", "args": {}},
        )[0]
        == "blocked"
    )
    assert classify_tool_request({}, {"name": "browser_click", "args": {}})[0] == "approved"
    assert (
        classify_tool_request({}, {"name": "browser_navigate", "args": {}})[0] == "approved"
    )
    assert (
        classify_tool_request({}, {"name": "payment_submit", "args": {}})[0]
        == "needs_human"
    )
    assert classify_tool_request({}, None)[0] == "blocked"


def test_policy_treats_canonical_browser_snapshot_as_snapshot_tool() -> None:
    decision, reason = classify_tool_request(
        {
            "snapshot": '- button "Search" ref=e7',
            "needs_fresh_snapshot": False,
            "last_tool": "browser_snapshot",
            "last_args": {},
        },
        {"name": "browser.snapshot", "args": {}},
    )

    assert decision == "blocked"
    assert "browser.snapshot is already current" in reason


def test_policy_allows_typing_target_validation_to_happen_in_agent_or_tool() -> None:
    decision, reason = classify_tool_request(
        {"snapshot": '- button "Search" ref=e7'},
        {"name": "browser_type", "args": {"ref": "e7", "text": "jackets"}},
    )

    assert decision == "approved"
    assert "browser.type" in reason


def test_policy_allows_ambiguous_generic_typing_targets() -> None:
    decision, reason = classify_tool_request(
        {"snapshot": '- generic "Search" [ref=f1e38]'},
        {"name": "browser_type", "args": {"target": "f1e38", "text": "jackets"}},
    )

    assert decision == "approved"
    assert "browser.type" in reason


def test_policy_allows_typing_into_textbox_ref() -> None:
    decision, reason = classify_tool_request(
        {"snapshot": '- textbox "Search" ref=e8'},
        {"name": "browser_type", "args": {"ref": "e8", "text": "jackets"}},
    )

    assert decision == "approved"
    assert "browser.type" in reason


def test_policy_block_increments_consecutive_failures() -> None:
    result = policy_node(
        {
            "snapshot": '- button "Search" ref=e7',
            "consecutive_failures": 1,
            "tool_request": {"name": "browser_snapshot", "args": {}},
        }
    )

    assert result["policy_decision"] == "blocked"
    assert result["consecutive_failures"] == 2
    assert "already current" in result["error"]


def test_policy_allows_deeper_snapshot_when_current_snapshot_exists() -> None:
    decision, reason = classify_tool_request(
        {
            "snapshot": '- generic "Catalog" ref=e7',
            "needs_fresh_snapshot": False,
            "last_tool": "browser_snapshot",
            "last_args": {},
        },
        {"name": "browser_snapshot", "args": {"depth": 5}},
    )

    assert decision == "approved"
    assert "browser.snapshot" in reason


def test_policy_blocks_snapshot_with_varied_depth_after_reuse_block() -> None:
    decision, reason = classify_tool_request(
        {
            "snapshot": '- generic "Catalog" ref=e7',
            "needs_fresh_snapshot": False,
            "last_tool": "browser_snapshot",
            "last_args": {"depth": 10},
            "observation": (
                "browser.snapshot is already current. Reuse the existing snapshot "
                "and refs instead of requesting another snapshot."
            ),
        },
        {"name": "browser_snapshot", "args": {"depth": 15}},
    )

    assert decision == "blocked"
    assert "varied depth" in reason


def test_policy_does_not_classify_ineffective_browser_actions() -> None:
    decision, reason = classify_tool_request(
        {
            "ineffective_browser_action": {
                "name": "browser_click",
                "args": {"target": "f1e38"},
            },
            "ineffective_action_count": 1,
        },
        {"name": "browser_click", "args": {"target": "f1e38"}},
    )

    assert decision == "approved"
    assert "browser.click" in reason


def test_policy_blocks_accumulated_ineffective_browser_actions() -> None:
    decision, reason = classify_tool_request(
        {"ineffective_action_count": 3},
        {"name": "browser_click", "args": {"target": "f1e38"}},
    )

    assert decision == "blocked"
    assert "did not change" in reason


@pytest.mark.asyncio
async def test_agent_node_replans_repeated_ineffective_browser_action() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"ref":"f1e38"},"reason":"Try again"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Search products",
            "plan": [{"id": 1, "description": "Open search", "status": "pending"}],
            "current_step": 0,
            "ineffective_browser_action": {
                "name": "browser_click",
                "args": {"target": "f1e38"},
            },
            "ineffective_action_count": 1,
        }
    )

    assert result["decision"] == "replan"
    assert "did not change" in result["observation"]


@pytest.mark.asyncio
async def test_agent_node_replans_ineffective_action_when_ref_changes() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_type","args":{"ref":"e19","text":"2000"},'
                '"reason":"Try price again"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Set price filter",
            "plan": [{"id": 1, "description": "Apply price filter", "status": "pending"}],
            "current_step": 0,
            "snapshot": '- textbox "Цена от" [ref=e19]',
            "ineffective_browser_actions": [
                {
                    "name": "browser_type",
                    "args": {"ref": "e5", "text": "2000"},
                    "target_description": 'textbox "Цена от"',
                }
            ],
            "ineffective_action_count": 1,
        }
    )

    assert result["decision"] == "replan"
    assert "did not change" in result["observation"]


@pytest.mark.asyncio
async def test_agent_node_allows_different_action_after_ineffective_action() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"ref":"f1e29"},"reason":"Try another control"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Search products",
            "plan": [{"id": 1, "description": "Open search", "status": "pending"}],
            "current_step": 0,
            "ineffective_browser_action": {
                "name": "browser_click",
                "args": {"target": "f1e38"},
            },
            "ineffective_action_count": 1,
        }
    )

    assert result["decision"] == "tool_call"
    assert result["tool_request"]["args"] == {"ref": "f1e29"}


@pytest.mark.asyncio
async def test_graph_stops_when_agent_decision_is_done() -> None:
    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            '{"decision":"done","final_answer":"finished"}',
        ]
    )

    def fail_policy_node(_state):
        raise AssertionError("Graph should stop before policy when decision is done")

    graph = build_agent_graph(llm=llm, tools=[], policy_node=fail_policy_node)

    result = await graph.ainvoke({"task": "Inspect"}, {"recursion_limit": 5})

    assert result["decision"] == "done"
    assert result["final_answer"] == "finished"


@pytest.mark.asyncio
async def test_graph_uses_injected_policy_node() -> None:
    def custom_policy_node(_state):
        return {"policy_decision": "blocked", "observation": "custom policy"}

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect page"}}'
            ),
            '{"decision":"done","final_answer":"policy handled"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[], policy_node=custom_policy_node)

    result = await graph.ainvoke({"task": "Inspect"}, {"recursion_limit": 10})

    assert result["final_answer"] == "policy handled"
    assert result["observation"] == "custom policy"


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


class FailingLLM:
    async def ainvoke(self, _messages):
        raise AssertionError("LLM should not be called")


class RecordingObserverLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content=self.response)


class ScriptedToolLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(self, _messages):
        if self.calls >= len(self.responses):
            return AIMessage(content=self.responses[-1])
        response = self.responses[self.calls]
        self.calls += 1
        return AIMessage(content=response)


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
        }
    )

    prompt = llm.messages[-1].content
    assert result["final_answer"] == "ok"
    assert "Original user request:\nInspect" in llm.messages[1].content
    assert "Search box found." in prompt
    assert 'textbox "Search" ref=e8' in prompt
    assert "e8" in prompt
    assert "[pending] Inspect page" in prompt
    assert "do not call browser.snapshot again with any\ndepth" in prompt
    assert all('textbox "Search" ref=e8' not in message.content for message in result["messages"])


@pytest.mark.asyncio
async def test_agent_node_forces_snapshot_after_invalid_ref() -> None:
    node = create_agent_node(FailingLLM())

    result = await node(
        {
            "task": "Click catalog",
            "plan": [{"id": 1, "description": "Click catalog", "status": "pending"}],
            "current_step": 0,
            "needs_fresh_snapshot": True,
            "error": "Ref e14 not found",
            "observation": "Tool failed.\n\nRef not found\n\nA fresh browser_snapshot is required.",
        }
    )

    assert result["decision"] == "tool_call"
    assert result["tool_request"]["name"] == "browser_snapshot"
    assert result["tool_request"]["args"] == {}
    assert result["needs_fresh_snapshot"] is False
    assert result["invalid_ref_recovery_count"] == 1
    assert result["error"] == "Ref e14 not found"
    assert result["messages"][-1].tool_calls[0]["name"] == "browser_snapshot"


@pytest.mark.asyncio
async def test_agent_node_tracks_stale_snapshot_retry_after_recovery() -> None:
    node = create_agent_node(FailingLLM())

    result = await node(
        {
            "task": "Click catalog",
            "plan": [{"id": 1, "description": "Click catalog", "status": "pending"}],
            "current_step": 0,
            "needs_fresh_snapshot": True,
            "error": "Ref e14 not found",
            "invalid_ref_recovery_count": 1,
        }
    )

    assert result["decision"] == "tool_call"
    assert result["tool_request"]["name"] == "browser_snapshot"
    assert result["needs_fresh_snapshot"] is False
    assert result["stale_snapshot_retries"] == 1
    assert result["invalid_ref_recovery_count"] == 2


@pytest.mark.asyncio
async def test_agent_node_replans_after_second_stale_snapshot_retry() -> None:
    node = create_agent_node(FailingLLM())

    result = await node(
        {
            "task": "Click catalog",
            "plan": [{"id": 1, "description": "Click catalog", "status": "pending"}],
            "current_step": 0,
            "needs_fresh_snapshot": True,
            "error": "Ref e14 not found",
            "invalid_ref_recovery_count": 2,
            "stale_snapshot_retries": 1,
        }
    )

    assert result["decision"] == "replan"
    assert result["stale_snapshot_retries"] == 0
    assert result["invalid_ref_recovery_count"] == 0
    assert result["needs_fresh_snapshot"] is False
    assert result["snapshot"] == ""
    assert "did not resolve the invalid ref twice" in result["observation"]


@pytest.mark.asyncio
async def test_agent_node_stops_on_third_identical_snapshot_request() -> None:
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
            "unchanged_snapshot_count": 2,
        }
    )

    assert result["decision"] == "done"
    assert "three consecutive times" in result["final_answer"]
    assert result["repeat_count"] == 3
    assert result["plan"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_node_stops_repeated_snapshot_even_after_prior_recovery() -> None:
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
            "unchanged_snapshot_count": 2,
        }
    )

    assert result["decision"] == "done"
    assert result["repeat_count"] == 3
    assert "three consecutive times" in result["final_answer"]
    assert result["plan"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_node_replans_repeated_snapshot_with_varied_depth() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{"depth":15},"reason":"Inspect deeper"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Inspect",
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "snapshot": '- heading "Alan Turing" ref=e1',
            "last_tool": "browser_snapshot",
            "last_args": {"depth": 10},
            "repeat_count": 2,
            "unchanged_snapshot_count": 2,
        }
    )

    assert result["decision"] == "done"
    assert result["repeat_count"] == 3
    assert "three consecutive times" in result["final_answer"]


@pytest.mark.asyncio
async def test_agent_node_replans_snapshot_after_reuse_policy_block() -> None:
    llm = FakeListLLM(
        responses=[
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{"depth":15},"reason":"Inspect deeper"}}'
            )
        ]
    )
    node = create_agent_node(llm)

    result = await node(
        {
            "task": "Inspect",
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "snapshot": '- heading "Alan Turing" ref=e1',
            "last_tool": "browser_snapshot",
            "last_args": {"depth": 10},
            "repeat_count": 1,
            "observation": (
                "browser.snapshot is already current. Reuse the existing snapshot "
                "and refs instead of requesting another snapshot."
            ),
            "policy_event": {
                "decision": "blocked",
                "reason": "browser.snapshot is already current.",
                "tool_request": {"name": "browser_snapshot", "args": {"depth": 10}},
            },
        }
    )

    assert result["decision"] == "replan"
    assert "current snapshot is already reusable" in result["observation"]


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


@pytest.mark.asyncio
async def test_executor_raw_tools_do_not_apply_playwright_mapping() -> None:
    calls: list[dict[str, str]] = []

    def browser_click(target: str) -> str:
        calls.append({"target": target})
        return "Clicked."

    node = create_executor_node(tools=[browser_click])
    result = await node(
        {
            "snapshot": '- button "Catalog" ref=e14',
            "tool_request": {"name": "browser_click", "args": {"ref": "e14"}},
        }
    )

    assert result["tool_result"]["status"] == "error"
    assert calls == []


@pytest.mark.asyncio
async def test_executor_maps_browser_ref_to_latest_mcp_target_via_provider() -> None:
    calls: list[dict[str, str]] = []

    @tool
    def browser_click(target: str) -> str:
        """Click a browser element."""

        calls.append({"target": target})
        return "Clicked."

    provider = PlaywrightMCPBrowserProvider([browser_click])
    node = create_executor_node(browser_providers=[provider])
    result = await node(
        {
            "snapshot": '- button "Catalog" ref=e14',
            "tool_request": {"name": "browser_click", "args": {"ref": "e14"}},
        }
    )

    assert result["tool_result"]["status"] == "success"
    assert calls == [{"target": "e14"}]


@pytest.mark.asyncio
async def test_executor_adds_element_for_legacy_ref_based_mcp_tool_via_provider() -> None:
    calls: list[dict[str, str]] = []

    @tool
    def browser_click(element: str, ref: str) -> str:
        """Click a browser element."""

        calls.append({"element": element, "ref": ref})
        return "Clicked."

    provider = PlaywrightMCPBrowserProvider([browser_click])
    node = create_executor_node(browser_providers=[provider])
    result = await node(
        {
            "snapshot": '- button "Catalog" ref=e14',
            "tool_request": {"name": "browser_click", "args": {"ref": "e14"}},
        }
    )

    assert result["tool_result"]["status"] == "success"
    assert calls == [{"element": 'button "Catalog"', "ref": "e14"}]


@pytest.mark.asyncio
async def test_executor_uses_registered_browser_provider_adapter() -> None:
    calls: list[dict[str, str]] = []

    @tool
    def browser_click(target: str) -> str:
        """Click a browser element."""

        calls.append({"target": target})
        return "Clicked."

    class FakeBrowserProvider:
        async def get_tools(self) -> list[Any]:
            return [browser_click]

        def normalize_request(self, request, state):
            if request.get("name") != "browser_click":
                return request
            return {**request, "args": {"target": "e14"}}

        def normalize_result(self, result):
            return result

    node = create_executor_node(tool_registry=ToolRegistry(providers=[FakeBrowserProvider()]))
    result = await node({"tool_request": {"name": "browser_click", "args": {"ref": "e14"}}})

    assert result["tool_result"]["status"] == "success"
    assert calls == [{"target": "e14"}]


def test_observe_node_translates_snapshot_and_advances_inspection_plan() -> None:
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
    assert "refs" not in result
    assert extract_element_refs(result["snapshot"]) == ["e7", "e8"]
    assert "Refs:" in result["observation"]
    assert result["current_step"] == 1
    assert result["plan"][0]["status"] == "completed"
    assert result["plan"][1]["status"] == "in_progress"
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
    assert result["plan"][0]["status"] == "completed"
    assert result["plan"][1]["status"] == "in_progress"


def test_observe_node_advances_plan_for_unicode_find_evidence() -> None:
    result = compile_observation(
        {
            "plan": [
                {"id": 1, "description": "Найти осенние куртки", "status": "pending"},
                {"id": 2, "description": "Extract product results", "status": "pending"},
            ],
            "current_step": 0,
            "tool_result": {
                "name": "browser_find",
                "status": "success",
                "content": 'Found 22 matches for "куртка"',
                "error": "",
            },
        }
    )

    assert result["current_step"] == 1
    assert result["plan"][0]["status"] == "completed"
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
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- button "New" ref=e2',
                "error": "",
            },
        }
    )

    assert result["snapshot"] == '- button "New" ref=e2'
    assert "refs" not in result
    assert extract_element_refs(result["snapshot"]) == ["e2"]
    assert "e1" not in result["observation"]


def test_observe_node_clears_snapshot_after_browser_action() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Search" ref=e7',
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )

    assert result["snapshot"] == ""
    assert "refs" not in result
    assert "Clicked" in result["observation"]


def test_observe_node_marks_action_ineffective_when_next_snapshot_is_unchanged() -> None:
    snapshot = '- generic "Search" ref=f1e29\n- button "Search" ref=f1e38'
    click_result = observe_node(
        {
            "snapshot": snapshot,
            "tool_request": {"name": "browser_click", "args": {"target": "f1e38"}},
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )
    snapshot_result = observe_node(
        {
            **click_result,
            "tool_request": {"name": "browser_snapshot", "args": {}},
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- generic "Search" ref=f1e29\n- button "Search" [active] ref=f1e38',
                "error": "",
            },
        }
    )

    assert snapshot_result["ineffective_browser_action"] == {
        "name": "browser_click",
        "args": {"target": "f1e38"},
        "target_description": 'button "Search"',
    }
    assert snapshot_result["ineffective_action_count"] == 1
    assert "did not change" in snapshot_result["observation"]


@pytest.mark.asyncio
async def test_observer_graph_preserves_ineffective_action_fields() -> None:
    snapshot = '- generic "Search" ref=f1e29\n- button "Search" ref=f1e38'
    graph = build_observer_graph()

    click_result = await graph.ainvoke(
        {
            "snapshot": snapshot,
            "tool_request": {"name": "browser_click", "args": {"target": "f1e38"}},
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )
    snapshot_result = await graph.ainvoke(
        {
            **click_result,
            "tool_request": {"name": "browser_snapshot", "args": {}},
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": snapshot,
                "error": "",
            },
        }
    )

    assert snapshot_result["ineffective_browser_action"] == {
        "name": "browser_click",
        "args": {"target": "f1e38"},
        "target_description": 'button "Search"',
    }
    assert snapshot_result["ineffective_action_count"] == 1


def test_observe_node_clears_ineffective_action_when_snapshot_changes() -> None:
    click_result = observe_node(
        {
            "snapshot": '- button "Search" ref=f1e38',
            "tool_request": {"name": "browser_click", "args": {"target": "f1e38"}},
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )
    snapshot_result = observe_node(
        {
            **click_result,
            "ineffective_browser_action": {
                "name": "browser_click",
                "args": {"target": "f1e38"},
            },
            "ineffective_action_count": 1,
            "tool_request": {"name": "browser_snapshot", "args": {}},
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- textbox "Search" ref=f1e40',
                "error": "",
            },
        }
    )

    assert snapshot_result["ineffective_browser_action"] == {}
    assert snapshot_result["ineffective_action_count"] == 0


def test_observe_node_stops_after_three_unchanged_snapshots() -> None:
    snapshot = '- button "Search" ref=e7'

    result = observe_node(
        {
            "snapshot": snapshot,
            "unchanged_snapshot_count": 2,
            "plan": [{"id": 1, "description": "Inspect page", "status": "pending"}],
            "current_step": 0,
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": snapshot,
                "error": "",
            },
        }
    )

    assert result["decision"] == "done"
    assert result["unchanged_snapshot_count"] == 3
    assert "same visible state" in result["final_answer"]


def test_observe_node_treats_snapshot_with_new_refs_as_unchanged() -> None:
    result = observe_node(
        {
            "snapshot": '- textbox "Цена от" [ref=e5]\n- link "Item" ref=e6',
            "unchanged_snapshot_count": 1,
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- textbox "Цена от" [ref=e19]\n- link "Item" ref=e27',
                "error": "",
            },
        }
    )

    assert result["unchanged_snapshot_count"] == 2


def test_observe_node_records_ineffective_action_when_only_ref_changes() -> None:
    result = observe_node(
        {
            "snapshot": '- textbox "Цена от" [ref=e19]',
            "snapshot_before_last_browser_action": '- textbox "Цена от" [ref=e5]',
            "last_browser_action": {
                "name": "browser_type",
                "args": {"ref": "e5", "text": "2000"},
                "target_description": 'textbox "Цена от"',
            },
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": '- textbox "Цена от" [ref=e19]',
                "error": "",
            },
        }
    )

    assert result["ineffective_action_count"] == 1
    assert result["ineffective_browser_actions"][0]["target_description"] == (
        'textbox "Цена от"'
    )


def test_observe_node_preserves_snapshot_after_non_ref_browser_error() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Search" [ref=f1e38]',
            "tool_result": {
                "name": "browser_click",
                "status": "error",
                "content": "",
                "error": "Timeout 5000ms exceeded.",
            },
        }
    )

    assert "snapshot" not in result
    assert "needs_fresh_snapshot" not in result
    assert "Timeout" in result["observation"]


def test_observe_node_requests_fresh_snapshot_after_stale_element_error() -> None:
    result = observe_node(
        {
            "snapshot": '- generic "Search" [ref=f1e38]',
            "tool_result": {
                "name": "browser_type",
                "status": "error",
                "content": "",
                "error": "Element is not editable.",
            },
        }
    )

    assert result["snapshot"] == ""
    assert result["needs_fresh_snapshot"] is True
    assert "fresh browser_snapshot" in result["observation"]


def test_observe_node_does_not_refresh_snapshot_after_failed_find_text() -> None:
    result = observe_node(
        {
            "snapshot": '- generic "Search" [ref=f1e38]',
            "tool_result": {
                "name": "browser_find",
                "status": "error",
                "content": "",
                "error": "Text input not found.",
            },
        }
    )

    assert "snapshot" not in result
    assert "needs_fresh_snapshot" not in result
    assert "fresh browser_snapshot" not in result["observation"]


def test_observe_node_resets_snapshot_retry_counters_after_successful_browser_action() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Search" ref=e7',
            "stale_snapshot_retries": 1,
            "invalid_ref_recovery_count": 2,
            "tool_result": {
                "name": "browser_click",
                "status": "success",
                "content": "Clicked.",
                "error": "",
            },
        }
    )

    assert result["stale_snapshot_retries"] == 0
    assert result["invalid_ref_recovery_count"] == 0


def test_observe_node_invalid_ref_is_plain_observation() -> None:
    result = observe_node(
        {
            "snapshot": '- button "Old" ref=e119',
            "tool_result": {
                "name": "browser_click",
                "status": "error",
                "content": "",
                "error": "Ref e119 not found",
            },
        }
    )

    assert result["snapshot"] == ""
    assert "refs" not in result
    assert result["needs_fresh_snapshot"] is True
    assert result["error"] == "Ref e119 not found"
    assert "Tool failed." in result["observation"]
    assert "Ref not found" in result["observation"]
    assert "fresh browser_snapshot" in result["observation"]


def test_observe_node_preserves_large_browser_snapshot_without_default_compression() -> None:
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
    assert "refs" not in result
    assert extract_element_refs(result["snapshot"]) == ["e42"]
    assert large_content in result["observation"]


def test_observe_node_can_compact_large_browser_snapshot() -> None:
    large_content = '- button "Save" ref=e42\n' + ("x" * (MAX_CONTENT_PREVIEW_CHARS + 500))

    result = compile_observation(
        {
            "tool_result": {
                "name": "browser_snapshot",
                "status": "success",
                "content": large_content,
                "error": "",
            },
        },
        compress_tool_output=True,
    )

    assert result["snapshot"] == large_content
    assert "refs" not in result
    assert extract_element_refs(result["snapshot"]) == ["e42"]
    assert len(result["observation"]) < len(large_content)


@pytest.mark.asyncio
async def test_observe_node_llm_receives_only_tool_result() -> None:
    observer_llm = RecordingObserverLLM(
        (
            '{"summary":"Snapshot compressed","visible_state":"Search box visible",'
            '"important_refs":["e8"],"errors":[],"next_observation_hint":""}'
        )
    )
    node = create_observe_node(observer_llm, compress_tools=True)

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
    assert "refs" not in result
    assert extract_element_refs(result["snapshot"]) == ["e8"]


@pytest.mark.asyncio
async def test_observe_node_appends_compact_tool_message_without_raw_snapshot() -> None:
    observer_llm = RecordingObserverLLM(
        (
            '{"summary":"Search input found.","visible_state":"Search input visible",'
            '"important_refs":["e8"],"errors":[],"next_observation_hint":""}'
        )
    )
    node = create_observe_node(observer_llm, compress_tools=True)
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
    node = create_observe_node(observer_llm, compress_tools=True)

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
async def test_graph_stream_stops_after_agent_done() -> None:
    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Answer directly","status":"pending"}]}',
            '{"decision":"done","final_answer":"finished"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[])

    chunks = [
        chunk
        async for chunk in graph.astream(
            {"task": "Finish"},
            {"recursion_limit": 10},
            stream_mode="updates",
        )
    ]

    assert chunks[-1]["agent"]["decision"] == "done"
    assert chunks[-1]["agent"]["final_answer"] == "finished"
    assert [next(iter(chunk)) for chunk in chunks] == ["plan", "agent"]


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
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot], compress_tools=True)

    result = await graph.ainvoke({"task": "Inspect"}, {"recursion_limit": 20})

    assert result["decision"] == "done"
    assert result["consecutive_failures"] == 3
    assert "Blocked: tool execution failed 3 consecutive times" in result["final_answer"]
    assert "connect ECONNREFUSED ::1:9222" in result["final_answer"]


@pytest.mark.asyncio
async def test_agent_node_stops_after_successful_steps_without_plan_advance() -> None:
    node = create_agent_node(FailingLLM())

    result = await node(
        {
            "task": "Apply price filter",
            "plan": [{"id": 1, "description": "Apply price filter", "status": "pending"}],
            "current_step": 0,
            "steps_without_plan_advance": 8,
            "observation": "browser_find returned success.\n\nNo matches.",
        }
    )

    assert result["decision"] == "done"
    assert "did not advance after 8 successful tool steps" in result["final_answer"]


@pytest.mark.asyncio
async def test_graph_breaks_invalid_ref_snapshot_loop() -> None:
    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return '- button "Catalog" ref=e14'

    @tool
    def browser_click(element: str, ref: str, button: str = "left") -> str:
        """Click a browser element."""

        raise RuntimeError(
            f"Ref {ref} not found in the current page snapshot. "
            "Try capturing new snapshot."
        )

    llm = ScriptedToolLLM(
        [
            '{"steps":[{"id":1,"description":"Click catalog","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect page"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"element":"Catalog","ref":"e14","button":"left"},'
                '"reason":"Click catalog"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"element":"Catalog","ref":"e14","button":"left"},'
                '"reason":"Click catalog again"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"element":"Catalog","ref":"e14","button":"left"},'
                '"reason":"Click catalog again"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_click","args":{"element":"Catalog","ref":"e14","button":"left"},'
                '"reason":"Click catalog third time"}}'
            ),
            '{"steps":[{"id":1,"description":"Try another way to open catalog","status":"pending"}]}',
            '{"decision":"done","final_answer":"Need another strategy for catalog."}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot, browser_click])

    result = await graph.ainvoke({"task": "Open catalog"}, {"recursion_limit": 40})

    assert result["decision"] == "done"
    assert result["replan_count"] == 1
    assert result["invalid_ref_recovery_count"] == 0
    assert result["stale_snapshot_retries"] == 0
    assert result["needs_fresh_snapshot"] is False
    assert result["final_answer"]


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
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot], compress_tools=True)

    result = await graph.ainvoke({"task": "Inspect"})

    assert result["final_answer"] == "observed"
    assert "Snapshot compressed" in result["observation"]
    assert result["snapshot"] == "snapshot text"
    assert result.get("current_step", 0) == 1
    assert result["messages"][0].type == "system"
    assert "Original user request:\nInspect" in result["messages"][1].content
    assert result["messages"][2].tool_calls[0]["name"] == "browser_snapshot"
    assert result["messages"][3].type == "tool"
    assert "Snapshot compressed" in result["messages"][3].content
    assert result["messages"][-1].content == "observed"
    assert "recovery_signal" not in result


@pytest.mark.asyncio
async def test_graph_tool_path_does_not_compress_by_default() -> None:
    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return '- button "Save" ref=e42'

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Inspect page","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Inspect page"}}'
            ),
            '{"decision":"done","final_answer":"observed raw"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot])

    result = await graph.ainvoke({"task": "Inspect"})

    assert result["final_answer"] == "observed raw"
    assert result["snapshot"] == '- button "Save" ref=e42'
    assert '- button "Save" ref=e42' in result["observation"]
    assert '- button "Save" ref=e42' in result["messages"][3].content


@pytest.mark.asyncio
async def test_graph_executor_subgraph_keeps_snapshot_for_legacy_browser_type() -> None:
    calls: list[dict[str, str]] = []

    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return '- textbox "Search" ref=e8'

    @tool
    def browser_type(element: str, ref: str, text: str) -> str:
        """Type into a browser element."""

        calls.append({"element": element, "ref": ref, "text": text})
        return "Typed."

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Search Ozon","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Find search input"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_type","args":{"ref":"e8","text":"iphone"},'
                '"reason":"Enter search query"}}'
            ),
            '{"decision":"done","final_answer":"typed"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot, browser_type])

    result = await graph.ainvoke({"task": "Search Ozon"}, {"recursion_limit": 12})

    assert result["final_answer"] == "typed"
    assert calls == [{"element": 'textbox "Search"', "ref": "e8", "text": "iphone"}]


@pytest.mark.asyncio
async def test_graph_executor_subgraph_accepts_canonical_browser_type() -> None:
    calls: list[dict[str, str]] = []

    @tool
    def browser_snapshot() -> str:
        """Return a browser snapshot."""

        return '- textbox "Search" ref=e8'

    @tool
    def browser_type(element: str, ref: str, text: str) -> str:
        """Type into a browser element."""

        calls.append({"element": element, "ref": ref, "text": text})
        return "Typed."

    llm = FakeListLLM(
        responses=[
            '{"steps":[{"id":1,"description":"Search Ozon","status":"pending"}]}',
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser_snapshot","args":{},"reason":"Find search input"}}'
            ),
            (
                '{"decision":"tool_call","tool_request":'
                '{"name":"browser.type","args":{"ref":"e8","text":"iphone"},'
                '"reason":"Enter search query"}}'
            ),
            '{"decision":"done","final_answer":"typed"}',
        ]
    )
    graph = build_agent_graph(llm=llm, tools=[browser_snapshot, browser_type])

    result = await graph.ainvoke({"task": "Search Ozon"}, {"recursion_limit": 12})

    assert result["final_answer"] == "typed"
    assert calls == [{"element": 'textbox "Search"', "ref": "e8", "text": "iphone"}]
