"""Deterministic context block assembly for agent prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from src.agent_loop.skills import browser_agent_rules_resource
from src.browser.names import is_browser_tool_name
from src.harness.tools import tool_name

ContextRole = Literal["system", "user", "developer"]


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
    """Build deterministic system and turn prompt context."""

    def assemble(
        self,
        state: Mapping[str, Any],
        *,
        tools: Sequence[Any] | None = None,
        blocks: Sequence[ContextBlock] | None = None,
    ) -> AssembledContext:
        """Assemble non-empty blocks from state, tools, and optional additions."""

        selected = list(blocks) if blocks is not None else self._state_blocks(state, tools)
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
            f"{block.name}:\n{block.content.strip()}" for block in selected
        )

    def _state_blocks(
        self,
        state: Mapping[str, Any],
        tools: Sequence[Any] | None,
    ) -> list[ContextBlock]:
        blocks = [
            ContextBlock(
                name="Task",
                role="user",
                content=str(state.get("task", "")),
                priority=10,
                source="state.task",
            ),
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
    "AssembledContext",
    "ContextAssembler",
    "ContextBlock",
    "ContextRole",
]
