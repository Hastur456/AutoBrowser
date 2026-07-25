from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError

from src.harness.runtime import BrowserHarness
from src.harness.runtime import HARNESS_STATE_OVERRIDES_CONFIG_KEY
from src.harness.context import ContextBuilder
from src.harness.memory import MemoryManager
from src.harness.policy import PolicyEngine
from src.harness.tools import ToolRegistry


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((state, config))
        return {"final_answer": "done", "state": state, "config": config}


class FailingGraph:
    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("graph failed")


class StreamingGraph(FakeGraph):
    async def astream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str,
    ):
        self.calls.append((state, config))
        yield {"plan": {"task": state["task"], "stream_mode": stream_mode}}
        yield {"agent": {"final_answer": "done"}}


class FakeStateSnapshot:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values


class RecursionAfterDoneGraph:
    def __init__(self) -> None:
        self.values = {"decision": "done", "final_answer": "finished"}

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        raise GraphRecursionError("Recursion limit reached")

    async def astream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str,
    ):
        yield {"plan": {"task": state["task"], "stream_mode": stream_mode}}
        yield {"agent": self.values}
        raise GraphRecursionError("Recursion limit reached")

    async def aget_state(self, config: dict[str, Any]) -> FakeStateSnapshot:
        return FakeStateSnapshot(self.values)


class FakeTool:
    name = "fake_tool"


class FakeCheckpointSaver:
    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


class CustomPolicyEngine(PolicyEngine):
    def classify_tool_request(self, state, request):  # type: ignore[override]
        return "blocked", "custom policy"


@pytest.mark.asyncio
async def test_browser_harness_injects_tools_and_memory() -> None:
    graph = FakeGraph()
    captured: dict[str, Any] = {}
    tools = [FakeTool()]
    context_builder = ContextBuilder(system_prompt="HARNESS PROMPT")

    def graph_builder(**kwargs: Any) -> FakeGraph:
        captured.update(kwargs)
        return graph

    harness = BrowserHarness(
        graph_builder,
        tools=tools,
        context_builder=context_builder,
        policy_engine=CustomPolicyEngine(),
        compress_tools=True,
    )

    result = await harness.run("inspect page", thread_id="test-thread")

    assert result["final_answer"] == "done"
    assert isinstance(captured["tool_registry"], ToolRegistry)
    assert await captured["tool_registry"].get_all() == tools
    assert captured["policy_node"]({"tool_request": {"name": "browser_snapshot"}})[
        "observation"
    ] == "custom policy"
    assert captured["checkpointer"] is harness.memory.get_checkpoint_saver()
    assert captured["compress_tools"] is True
    assert graph.calls[0][0] == {"task": "inspect page"}
    assert graph.calls[0][1]["configurable"]["thread_id"] == "test-thread"

    history = captured["history_builder"]({"task": "inspect page"})
    assert isinstance(history[0], SystemMessage)
    assert history[0].content == "HARNESS PROMPT"
    assert isinstance(history[1], HumanMessage)

    next_history = captured["history_builder"](
        {"task": "open the first result", "task_id": "task-2", "messages": history}
    )
    assert next_history[:2] == history
    assert next_history[2].content == "User request (task-2):\nopen the first result"
    assert captured["history_builder"](
        {"task": "open the first result", "task_id": "task-2", "messages": next_history}
    ) == next_history


@pytest.mark.asyncio
async def test_browser_harness_preserves_existing_config() -> None:
    graph = FakeGraph()

    harness = BrowserHarness(lambda **kwargs: graph)

    result = await harness.run(
        "inspect page",
        config={"recursion_limit": 5, "configurable": {"checkpoint_ns": "cli"}},
        thread_id="test-thread",
    )

    assert result["config"]["recursion_limit"] == 5
    assert result["config"]["configurable"] == {
        "checkpoint_ns": "cli",
        "thread_id": "test-thread",
    }


@pytest.mark.asyncio
async def test_browser_harness_applies_internal_state_overrides() -> None:
    graph = FakeGraph()
    prior_messages = [HumanMessage(content="prior task")]
    harness = BrowserHarness(lambda **kwargs: graph)

    result = await harness.run(
        "next task",
        config={
            "configurable": {"thread_id": "test-thread"},
            HARNESS_STATE_OVERRIDES_CONFIG_KEY: {
                "messages": prior_messages,
                "snapshot": "- link result ref=e1",
                "decision": "",
            },
        },
    )

    assert result["state"] == {
        "task": "next task",
        "messages": prior_messages,
        "snapshot": "- link result ref=e1",
        "decision": "",
    }
    assert HARNESS_STATE_OVERRIDES_CONFIG_KEY not in result["config"]


@pytest.mark.asyncio
async def test_browser_harness_streams_updates() -> None:
    graph = StreamingGraph()
    harness = BrowserHarness(lambda **kwargs: graph)

    chunks = [
        chunk
        async for chunk in harness.stream_updates("inspect page", thread_id="test-thread")
    ]

    assert chunks == [
        {"plan": {"task": "inspect page", "stream_mode": "updates"}},
        {"agent": {"final_answer": "done"}},
    ]
    assert graph.calls[0][1]["configurable"]["thread_id"] == "test-thread"


@pytest.mark.asyncio
async def test_memory_manager_deletes_task_thread() -> None:
    saver = FakeCheckpointSaver()
    memory = MemoryManager(checkpoint_saver=saver)

    await memory.delete_thread("task-123")

    assert saver.deleted_threads == ["task-123"]


@pytest.mark.asyncio
async def test_browser_harness_logs_graph_errors(caplog) -> None:
    harness = BrowserHarness(lambda **kwargs: FailingGraph())

    with pytest.raises(RuntimeError, match="graph failed"):
        await harness.run("inspect page", thread_id="test-thread")

    assert "Harness error: graph failed" in caplog.text


@pytest.mark.asyncio
async def test_browser_harness_returns_completed_state_after_recursion_boundary() -> None:
    graph = RecursionAfterDoneGraph()
    harness = BrowserHarness(lambda **kwargs: graph)

    result = await harness.run("inspect page", thread_id="test-thread")

    assert result == {"decision": "done", "final_answer": "finished"}


@pytest.mark.asyncio
async def test_browser_harness_stream_suppresses_recursion_after_done() -> None:
    graph = RecursionAfterDoneGraph()
    harness = BrowserHarness(lambda **kwargs: graph)

    chunks = [
        chunk
        async for chunk in harness.stream_updates("inspect page", thread_id="test-thread")
    ]

    assert chunks == [
        {"plan": {"task": "inspect page", "stream_mode": "updates"}},
        {"agent": {"decision": "done", "final_answer": "finished"}},
    ]
