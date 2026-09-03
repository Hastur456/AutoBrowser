"""Harness composition root for the engine-native AutoBrowser execution loop.

Control flow lives in the explicit engine (:mod:`src.agent_loop.execution.loop`), so
:class:`BrowserHarness` is a pure composition root: it holds the infrastructure collaborators
(telemetry, events, prompt context, tool registry, policy, and the reasoning ``llm``) that
:meth:`~src.agent_loop.execution.resources.EngineResources.from_harness` reads to drive one goal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.agent_loop.context import ContextAssembler
from src.agent_loop.events import EventEmitter
from src.harness.policy import PolicyEngine
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolLoader, ToolRegistry

HARNESS_STATE_OVERRIDES_CONFIG_KEY = "_autobrowser_state_overrides"
HARNESS_EVENT_METADATA_CONFIG_KEY = "_autobrowser_event_metadata"


class BrowserHarness:
    """Compose the harness infrastructure the engine-native execution loop consumes.

    The engine reaches these collaborators through
    :meth:`~src.agent_loop.execution.resources.EngineResources.from_harness`, which reads
    ``tools`` (the :class:`~src.harness.tools.ToolRegistry`), ``policy``, ``context`` (the
    :class:`~src.agent_loop.context.ContextAssembler` that is the sole prompt-construction
    boundary),
    ``events``. The reasoning ``llm`` is stored here for convenience but is
    supplied explicitly to ``from_harness`` by ``SessionRuntime.run_task``.
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        tools: Sequence[Any] | None = None,
        tool_loader: ToolLoader | None = None,
        tool_registry: ToolRegistry | None = None,
        context_assembler: ContextAssembler | None = None,
        telemetry: TelemetryObserver | None = None,
        policy_engine: PolicyEngine | None = None,
        event_emitter: EventEmitter | None = None,
        compress_tools: bool = False,
    ) -> None:
        self.telemetry = telemetry or TelemetryObserver()
        self.events = event_emitter or EventEmitter()
        self.context = context_assembler or ContextAssembler()
        self.tools = tool_registry or ToolRegistry(tools=tools, tool_loader=tool_loader)
        self.policy = policy_engine or PolicyEngine()
        self.llm = llm
        self.compress_tools = compress_tools


__all__ = [
    "HARNESS_EVENT_METADATA_CONFIG_KEY",
    "HARNESS_STATE_OVERRIDES_CONFIG_KEY",
    "BrowserHarness",
]
