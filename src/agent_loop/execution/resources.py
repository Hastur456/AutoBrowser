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
from dataclasses import dataclass, field
from typing import Any

from src.harness.hooks import NullHookEngine
from src.harness.normalization import ToolCallNormalizer
from src.harness.permissions import PermissionEngine
from src.harness.tools import ToolRegistry


@dataclass(frozen=True)
class EngineResources:
    """Immutable bundle of the collaborators the native execution loop needs.

    ``context`` is the :class:`~src.agent_loop.context.ContextAssembler` (the sanctioned
    prompt-assembly boundary that owns ``get_system_prompt``/``user_turn_prompt``/
    ``plan_prompt`` and knows about the agent/planner prompts); ``events`` is the session ``EventEmitter`` whose sink
    chain applies redaction and whose ``sequence`` the goal watchdog polls. The progress
    guard is not a resource: the loop calls the pure functions in
    :mod:`src.agent_loop.execution.guards` directly. ``hooks`` is the session's
    :class:`~src.harness.hooks.HookEngine`; the default :class:`~src.harness.hooks.NullHookEngine`
    runs nothing, so evals and tests that do not pass one never see hooks. ``permissions`` is
    the session's :class:`~src.harness.permissions.PermissionEngine`; the default one is built
    from the code-default settings (no rules, ``default`` mode, no config is read), so every
    call runs unless a test passes its own engine.
    """

    llm: Any
    tool_registry: ToolRegistry
    tool_normalizers: Sequence[ToolCallNormalizer]
    context: Any
    events: Any
    hooks: Any = field(default_factory=NullHookEngine)
    permissions: PermissionEngine = field(default_factory=PermissionEngine.from_settings)

    @classmethod
    def from_harness(
        cls,
        harness: Any,
        *,
        llm: Any,
        events: Any | None = None,
        hooks: Any | None = None,
        permissions: PermissionEngine | None = None,
    ) -> EngineResources:
        """Compose resources from an initialized ``BrowserHarness`` plus the ``llm``.

        ``events`` defaults to ``harness.events`` but can be overridden so the loop emits
        through the exact same ``EventEmitter`` the enclosing ``GoalRunner`` watchdog polls
        (``SessionContext.event_emitter``), guaranteeing progress is observed. ``hooks`` is
        not a harness concern: the session passes its own ``HookEngine``; without it the
        loop gets a :class:`~src.harness.hooks.NullHookEngine`. ``permissions`` is session-scoped
        too (``SessionContext.permissions``); without it the default engine applies.
        """

        tool_registry = harness.tools
        return cls(
            llm=llm,
            tool_registry=tool_registry,
            # Same source the ToolBroker folds around every call.
            tool_normalizers=list(getattr(tool_registry, "get_normalizers", list)()),
            context=harness.context,
            events=events if events is not None else harness.events,
            hooks=hooks if hooks is not None else NullHookEngine(),
            permissions=permissions if permissions is not None else PermissionEngine.from_settings(),
        )


__all__ = ["EngineResources"]
