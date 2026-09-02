"""Resource bundle for the engine-native execution loop.

:class:`EngineResources` gathers exactly what :class:`~src.agent_loop.execution.loop.AgentExecutionLoop`
needs to drive one goal — without a graph or a harness rewrite. It composes objects the
harness already owns (tool registry, browser providers, prompt context, event emitter)
plus the reasoning ``llm``, which the harness does **not** store on itself
(``BrowserHarness.__init__`` passes ``llm`` straight into the graph builder), so it is
supplied separately by the caller in ``SessionRuntime.run_task``.

This module imports nothing from ``src/agent/``: it depends only on the harness and browser
layers, matching the decoupling rule for the whole ``src/agent_loop/execution/`` package.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.browser import BrowserProvider
from src.harness.tools import ToolRegistry


@dataclass(frozen=True)
class EngineResources:
    """Immutable bundle of the collaborators the native execution loop needs.

    ``context`` is the harness :class:`~src.harness.context.ContextBuilder` (the sanctioned
    prompt-assembly boundary that owns ``build_turn_prompt``/``build_plan_prompt`` and knows
    about the agent/planner prompts); ``events`` is the session ``EventEmitter`` whose sink
    chain applies redaction and whose ``sequence`` the goal watchdog polls; ``policy`` is the
    legacy ``PolicyEngine`` carried for inspection only (native classification uses the pure
    functions in :mod:`src.agent_loop.execution.policy`, not this object).
    """

    llm: Any
    tool_registry: ToolRegistry
    browser_providers: Sequence[BrowserProvider]
    policy: Any
    context: Any
    events: Any

    @classmethod
    def from_harness(
        cls,
        harness: Any,
        *,
        llm: Any,
        events: Any | None = None,
    ) -> "EngineResources":
        """Compose resources from an initialized ``BrowserHarness`` plus the ``llm``.

        ``events`` defaults to ``harness.events`` but can be overridden so the loop emits
        through the exact same ``EventEmitter`` the enclosing ``GoalRunner`` watchdog polls
        (``SessionContext.event_emitter``), guaranteeing progress is observed.
        """

        tool_registry = harness.tools
        return cls(
            llm=llm,
            tool_registry=tool_registry,
            browser_providers=list(tool_registry.get_browser_providers()),
            policy=getattr(harness, "policy", None),
            context=harness.context,
            events=events if events is not None else harness.events,
        )


__all__ = ["EngineResources"]
