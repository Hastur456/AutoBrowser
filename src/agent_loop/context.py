"""Deterministic context assembly: the single prompt-construction boundary.

:class:`ContextAssembler` is the only implementation responsible for prompt
construction: it owns the durable system prompt, builds ordered, typed context
blocks for a turn (:meth:`ContextAssembler.assemble`), and renders the planner
prompt (:meth:`ContextAssembler.plan_prompt`). The legacy ``ContextBuilder`` in
``src/harness/context.py`` and the ``legacy``/``assembled`` context-mode switch
were removed — there is one execution path and one way to build prompts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from src.agent_loop.prompts import (
    AGENT_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_PROMPT,
)
from src.agent_loop.skills import browser_agent_rules_resource
from src.browser.names import is_browser_tool_name
from src.config import get_settings
from src.harness.tools import tool_name

ContextRole = Literal["system", "user", "developer"]

# Closing directive appended to the assembled per-turn user prompt.
ACTION_INSTRUCTION = "Choose the next action."

# Heading of the persistent-memory block (rendered by the session's MemoryContext).
MEMORY_BLOCK_NAME = "Memory"
# Marker appended to a block cut to its ``token_budget``.
TRUNCATED_BLOCK_SUFFIX = "\n... [truncated to fit the context budget]"


@dataclass(frozen=True)
class ContextBlock:
    """A named, renderable unit of model context."""

    name: str
    role: ContextRole
    content: str
    priority: int = 100
    source: str = "runtime"
    token_budget: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Return whether this block has no meaningful content."""

        return not self.content.strip()


@dataclass(frozen=True)
class AssembledContext:
    """Ordered context blocks and their rendered prompt views."""

    system_prompt: str
    turn_prompt: str
    blocks: tuple[ContextBlock, ...]


class ContextAssembler:
    """Build deterministic system, turn, and plan prompt context.

    This is the sanctioned prompt-assembly boundary for the engine-native loop:
    it knows the agent system prompt and the agent/planner prompts, so the engine
    never imports prompt resources itself.
    """

    def __init__(self, system_prompt: str | None = None) -> None:
        self._system_prompt = system_prompt or AGENT_SYSTEM_PROMPT

    def get_system_prompt(self) -> str:
        """Return the system prompt injected into durable message history."""

        return self._system_prompt

    def assemble(
        self,
        state: Mapping[str, Any],
        *,
        tools: Sequence[Any] | None = None,
        blocks: Sequence[ContextBlock] | None = None,
        memory: str = "",
    ) -> AssembledContext:
        """Assemble non-empty blocks from state, tools, and optional additions.

        ``memory`` is the pre-rendered persistent-memory text (the engine gets it from
        ``EngineResources.memory``); empty means no ``Memory`` block.
        """

        selected = (
            list(blocks) if blocks is not None else self._state_blocks(state, tools, memory)
        )
        ordered = tuple(
            sorted(
                (block for block in selected if not block.is_empty()),
                key=lambda block: (block.priority, block.name, block.source),
            )
        )
        return AssembledContext(
            system_prompt=self.render(ordered, roles={"system", "developer"}),
            turn_prompt=self.render(ordered, roles={"user"}),
            blocks=ordered,
        )

    def render(
        self,
        blocks: Sequence[ContextBlock],
        *,
        roles: set[ContextRole] | None = None,
    ) -> str:
        """Render blocks with stable headings, optionally filtered by role."""

        selected = [
            block
            for block in blocks
            if roles is None or block.role in roles
        ]
        return "\n\n".join(
            f"{block.name}:\n{_fit(block)}" for block in selected
        )

    def user_turn_prompt(
        self,
        state: Mapping[str, Any],
        *,
        tools: Sequence[Any] | None = None,
        memory: str = "",
    ) -> str:
        """Return the assembled per-turn user prompt for one agent step.

        The rendered user blocks are closed with the action instruction so the
        model always sees the decision directive even when no state block has
        meaningful content. A non-empty ``memory`` adds the ``Memory`` block.
        """

        prompt = self.assemble(state, tools=tools, memory=memory).turn_prompt.strip()
        if not prompt:
            return ACTION_INSTRUCTION
        return f"{prompt}\n\n{ACTION_INSTRUCTION}"

    def plan_prompt(self, state: Mapping[str, Any], *, memory: str = "") -> str:
        """Return the planner prompt for the current state.

        The planner message is the ``PLANNER_SYSTEM_PROMPT`` and ``PLANNER_USER_PROMPT``
        joined by a blank line, with the observation defaulting to
        ``"No observation yet."``. A non-empty ``memory`` is appended as a separate
        ``Memory`` section. This is the sanctioned prompt-assembly boundary for the
        engine-native loop, which must not import planner prompts from the engine itself.
        """

        task = str(state.get("task", "") or "").strip()
        observation = str(state.get("observation", "") or "")
        parts = [
            PLANNER_SYSTEM_PROMPT,
            PLANNER_USER_PROMPT.format(
                task=task,
                observation=observation or "No observation yet.",
            ),
        ]
        block = _memory_block(memory)
        if not block.is_empty():
            parts.append(self.render([block]))
        return "\n\n".join(parts)

    def _state_blocks(
        self,
        state: Mapping[str, Any],
        tools: Sequence[Any] | None,
        memory: str = "",
    ) -> list[ContextBlock]:
        blocks = [
            ContextBlock(
                name="Task",
                role="user",
                content=str(state.get("task", "")),
                priority=10,
                source="state.task",
            ),
            _memory_block(memory),
            ContextBlock(
                name="Plan",
                role="user",
                content=_format_plan(state.get("plan")),
                priority=20,
                source="state.plan",
            ),
            ContextBlock(
                name="Observation",
                role="user",
                content=str(state.get("observation", "")),
                priority=30,
                source="state.observation",
            ),
            # Pre-rendered journal of this task's tool calls and their outcomes (see
            # ``execution/progress.py``); survives history compaction, empty on turn one.
            ContextBlock(
                name="Action History",
                role="user",
                content=str(state.get("action_history", "") or ""),
                priority=25,
                source="state.action_history",
            ),
            # The model's own task-local notes (the optional ``notes`` field of its decision).
            ContextBlock(
                name="Working Notes",
                role="user",
                content=str(state.get("working_notes", "") or ""),
                priority=26,
                source="state.working_notes",
            ),
        ]
        if tools:
            blocks.append(
                ContextBlock(
                    name="Tool Inventory",
                    role="system",
                    content=_format_tools(tools),
                    priority=40,
                    source="tool_registry",
                )
            )
        if _is_browser_relevant(state, tools):
            resource = browser_agent_rules_resource()
            blocks.append(
                ContextBlock(
                    name="Browser Rules",
                    role="system",
                    content=resource.load(),
                    priority=45,
                    source=f"resource:{resource.name}",
                    metadata={"resource": resource.name},
                )
            )
        return blocks


def _memory_block(memory: str) -> ContextBlock:
    """The persistent-memory block, between ``Task`` (10) and ``Plan`` (20)."""

    return ContextBlock(
        name=MEMORY_BLOCK_NAME,
        role="user",
        content=str(memory or ""),
        priority=15,
        source="memory",
        token_budget=get_settings().memory.block_max_chars if memory else None,
    )


def _fit(block: ContextBlock) -> str:
    """The block's content, cut to its ``token_budget`` (characters) when it has one."""

    content = block.content.strip()
    budget = block.token_budget
    if budget is None or len(content) <= budget:
        return content
    keep = max(0, budget - len(TRUNCATED_BLOCK_SUFFIX))
    return content[:keep].rstrip() + TRUNCATED_BLOCK_SUFFIX


def _format_plan(plan: Any) -> str:
    if not plan:
        return ""
    if isinstance(plan, str):
        return plan
    if not isinstance(plan, Sequence):
        return str(plan)
    return "\n".join(
        f"{step.get('id', index)}. [{step.get('status', 'pending')}] "
        f"{step.get('description', '')}"
        for index, step in enumerate(plan, start=1)
        if isinstance(step, Mapping)
    )


def _format_tools(tools: Sequence[Any] | None) -> str:
    if not tools:
        return ""
    lines = []
    for tool in tools:
        name = tool_name(tool)
        if not name:
            continue
        description = str(getattr(tool, "description", "") or "").strip()
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    return "\n".join(sorted(lines))


def _is_browser_relevant(
    state: Mapping[str, Any],
    tools: Sequence[Any] | None,
) -> bool:
    if any(is_browser_tool_name(tool_name(tool)) for tool in tools or []):
        return True
    if any(
        state.get(key)
        for key in (
            "snapshot",
            "browser",
            "last_browser_action",
            "ineffective_browser_action",
            "pending_browser_tab_index",
        )
    ):
        return True
    task = str(state.get("task", "")).lower()
    return any(
        term in task
        for term in browser_agent_rules_resource().trigger_terms
    )


__all__ = [
    "ACTION_INSTRUCTION",
    "MEMORY_BLOCK_NAME",
    "AssembledContext",
    "ContextAssembler",
    "ContextBlock",
    "ContextRole",
]
