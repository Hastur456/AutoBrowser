"""Tests for the ``BrowserHarness`` composition root and ``EngineResources`` bundling.

The legacy graph streaming/recursion-recovery behavior was removed along with
``src/agent/``: ``BrowserHarness`` is now a pure composition root that holds the
infrastructure collaborators the engine-native loop consumes. These tests cover its
default wiring, injection, tool-registry composition, and how
:meth:`src.agent_loop.execution.resources.EngineResources.from_harness` reads it.
"""

from __future__ import annotations

import pytest

from src.agent_loop.context import ContextAssembler
from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.execution.resources import EngineResources
from src.browser import FakeBrowserProvider
from src.harness.policy import PolicyEngine
from src.harness.runtime import BrowserHarness
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolRegistry


class FakeTool:
    name = "fake_tool"


class CustomPolicyEngine(PolicyEngine):
    def classify_tool_request(self, state, request):  # type: ignore[override]
        return "blocked", "custom policy"


def test_browser_harness_wires_default_collaborators() -> None:
    harness = BrowserHarness()

    assert isinstance(harness.telemetry, TelemetryObserver)
    assert isinstance(harness.events, EventEmitter)
    assert isinstance(harness.context, ContextAssembler)
    assert isinstance(harness.tools, ToolRegistry)
    assert isinstance(harness.policy, PolicyEngine)
    assert harness.llm is None
    assert harness.compress_tools is False


def test_browser_harness_preserves_injected_collaborators() -> None:
    tools = [FakeTool()]
    tool_registry = ToolRegistry(tools=tools)
    context_assembler = ContextAssembler(system_prompt="HARNESS PROMPT")
    policy_engine = CustomPolicyEngine()
    telemetry = TelemetryObserver()
    events = EventEmitter(InMemoryEventSink(), session_id="session-1")
    llm = object()

    harness = BrowserHarness(
        llm=llm,
        tool_registry=tool_registry,
        context_assembler=context_assembler,
        telemetry=telemetry,
        policy_engine=policy_engine,
        event_emitter=events,
        compress_tools=True,
    )

    assert harness.tools is tool_registry
    assert harness.context is context_assembler
    assert harness.policy is policy_engine
    assert harness.telemetry is telemetry
    assert harness.events is events
    assert harness.llm is llm
    assert harness.compress_tools is True


@pytest.mark.asyncio
async def test_browser_harness_composes_tool_registry_from_tools() -> None:
    tools = [FakeTool()]

    harness = BrowserHarness(tools=tools)

    assert await harness.tools.get_all() == tools
    assert "fake_tool" in await harness.tools.get_by_name()


@pytest.mark.asyncio
async def test_engine_resources_from_harness_bundles_collaborators() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])
    tools = [FakeTool()]
    tool_registry = ToolRegistry(tools=tools, providers=[provider])
    context_assembler = ContextAssembler(system_prompt="HARNESS PROMPT")
    policy_engine = CustomPolicyEngine()
    events = EventEmitter(InMemoryEventSink(), session_id="session-1")
    harness = BrowserHarness(
        tool_registry=tool_registry,
        context_assembler=context_assembler,
        policy_engine=policy_engine,
        event_emitter=events,
    )
    llm = object()

    resources = EngineResources.from_harness(harness, llm=llm)

    assert resources.llm is llm
    assert resources.tool_registry is tool_registry
    assert resources.browser_providers == [provider]
    assert resources.policy is policy_engine
    assert resources.context is context_assembler
    assert resources.events is events


def test_engine_resources_from_harness_overrides_events() -> None:
    harness = BrowserHarness()
    session_events = EventEmitter(InMemoryEventSink(), session_id="session-1")

    resources = EngineResources.from_harness(harness, llm=object(), events=session_events)

    assert resources.events is session_events
