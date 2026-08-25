"""Harness composition root for the engine-native AutoBrowser execution loop.

Historically this module built and owned the compiled LangGraph graph and exposed
``run``/``stream_updates``/``get_state_values`` over it. Control flow now lives in the explicit
engine (:mod:`src.agent_loop.execution.loop`), so :class:`BrowserHarness` is a pure composition
root: it holds the infrastructure collaborators (telemetry, events, memory, prompt context, tool
registry, policy, and the reasoning ``llm``) that
:meth:`~src.agent_loop.execution.resources.EngineResources.from_harness` reads to drive one goal.
It imports nothing from ``src/agent/``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.agent_loop.events import EventEmitter
from src.agent_loop.tracing import EVENT_METADATA_CONFIG_KEY
from src.harness.context import ContextBuilder
from src.harness.memory import MemoryManager
from src.harness.policy import PolicyEngine
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolLoader, ToolRegistry

HARNESS_STATE_OVERRIDES_CONFIG_KEY = "_autobrowser_state_overrides"
HARNESS_EVENT_METADATA_CONFIG_KEY = EVENT_METADATA_CONFIG_KEY


class BrowserHarness:
    """Compose the harness infrastructure the engine-native execution loop consumes.

    The engine reaches these collaborators through
    :meth:`~src.agent_loop.execution.resources.EngineResources.from_harness`, which reads
    ``tools`` (the :class:`~src.harness.tools.ToolRegistry`), ``policy``, ``context`` (the
    :class:`~src.harness.context.ContextBuilder` that owns the agent/planner prompts),
    ``events`` and ``memory``. The reasoning ``llm`` is stored here for convenience but is
    supplied explicitly to ``from_harness`` by ``SessionRuntime.run_task``.
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        tools: Sequence[Any] | None = None,
        tool_loader: ToolLoader | None = None,
        tool_registry: ToolRegistry | None = None,
        memory_manager: MemoryManager | None = None,
        context_builder: ContextBuilder | None = None,
        telemetry: TelemetryObserver | None = None,
        policy_engine: PolicyEngine | None = None,
        event_emitter: EventEmitter | None = None,
        compress_tools: bool = False,
    ) -> None:
        self.telemetry = telemetry or TelemetryObserver()
        self.events = event_emitter or EventEmitter()
        self.memory = memory_manager or MemoryManager()
        self.context = context_builder or ContextBuilder()
        self.tools = tool_registry or ToolRegistry(tools=tools, tool_loader=tool_loader)
        self.policy = policy_engine or PolicyEngine()
        self.llm = llm
        self.compress_tools = compress_tools


__all__ = [
    "BrowserHarness",
    "HARNESS_EVENT_METADATA_CONFIG_KEY",
    "HARNESS_STATE_OVERRIDES_CONFIG_KEY",
]
