"""Resource bundle for the engine-native execution loop.

:class:`EngineResources` gathers exactly what :class:`~src.agent_loop.execution.loop.AgentExecutionLoop`
needs to drive one goal. It composes objects the harness already owns (tool registry and
its normalizers, prompt context, event emitter), the session-scoped collaborators (hooks,
permissions, approval judge, memory, tool roles) and the reasoning ``llm``, which the
caller (``SessionRuntime.run_task``) supplies explicitly.

Like the whole ``src/agent_loop/execution/`` package, this module imports nothing from
``src/browser/``: server-specific behaviour arrives as the objects bundled here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.agent_loop.context import ContextAssembler
from src.agent_loop.events import EventEmitter
from src.agent_loop.execution.approval import ApprovalJudge
from src.contracts import MemoryRenderer, ToolRoles
from src.harness.hooks import HookEngine, NullHookEngine
from src.harness.memory import NullMemoryContext
from src.harness.normalization import ToolCallNormalizer
from src.harness.permissions import PermissionEngine
from src.harness.tools import NullToolRoles, ToolRegistry
from src.llm import ChatModel

if TYPE_CHECKING:
    from src.harness.runtime import BrowserHarness


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
    call runs unless a test passes its own engine. ``approval`` is the session's
    :class:`~src.agent_loop.execution.approval.ApprovalJudge` (model judgments that escalate a
    call to human approval); the default judges nothing. ``memory`` renders the persistent
    ``Memory`` context block (``render(state) -> str``, the session's ``MemoryContext``);
    the default renders nothing, so evals and
    tests never see the developer's memory files. ``tool_roles`` tells the observation
    compiler which tool returns the environment snapshot and which tools act on it; the
    default :class:`~src.harness.tools.NullToolRoles` assigns none, so no snapshot is kept.
    """

    llm: ChatModel
    tool_registry: ToolRegistry
    tool_normalizers: Sequence[ToolCallNormalizer]
    context: ContextAssembler
    events: EventEmitter
    hooks: HookEngine | NullHookEngine = field(default_factory=NullHookEngine)
    permissions: PermissionEngine = field(default_factory=PermissionEngine.from_settings)
    approval: ApprovalJudge = field(default_factory=ApprovalJudge)
    memory: MemoryRenderer = field(default_factory=NullMemoryContext)
    tool_roles: ToolRoles = field(default_factory=NullToolRoles)

    @classmethod
    def from_harness(
        cls,
        harness: BrowserHarness,
        *,
        llm: ChatModel,
        events: EventEmitter | None = None,
        hooks: HookEngine | NullHookEngine | None = None,
        permissions: PermissionEngine | None = None,
        approval: ApprovalJudge | None = None,
        memory: MemoryRenderer | None = None,
        tool_roles: ToolRoles | None = None,
    ) -> EngineResources:
        """Compose resources from an initialized ``BrowserHarness`` plus the ``llm``.

        ``events`` defaults to ``harness.events`` but can be overridden so the loop emits
        through the exact same ``EventEmitter`` the enclosing ``GoalRunner`` watchdog polls
        (``SessionContext.event_emitter``), guaranteeing progress is observed. ``hooks`` is
        not a harness concern: the session passes its own ``HookEngine``; without it the
        loop gets a :class:`~src.harness.hooks.NullHookEngine`. ``permissions`` is session-scoped
        too (``SessionContext.permissions``); without it the default engine applies. So is
        ``approval`` (``SessionContext.approval``); without it nothing is judged. So is
        ``memory`` (``SessionContext.memory``); without it no ``Memory`` block is rendered.
        So are ``tool_roles`` (``SessionContext.tool_roles``); without them no snapshot is
        kept.
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
            approval=approval if approval is not None else ApprovalJudge(),
            memory=memory if memory is not None else NullMemoryContext(),
            tool_roles=tool_roles if tool_roles is not None else NullToolRoles(),
        )


__all__ = ["EngineResources"]
