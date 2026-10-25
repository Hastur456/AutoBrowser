"""``skill_view``: the model loads a skill body or a skill file on demand.

Shaped like :class:`~src.harness.memory_tool.MemoryTool` (``name``, ``description``,
``input_schema``, ``server``, ``annotations``, async ``invoke``), so the tool broker, the
permission engine (``check.server == "skills"``), ``tool_is_read_only`` and the model tool
schemas handle it without any special case. The engine never names it.

``readOnlyHint``: allowed in ``read_only`` permission mode. A bad name or path raises; the
broker turns it into a tool error the model reads (the turn is not terminal). Skills are only
read — nothing in a skill folder is ever executed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.harness.skills import SKILL_FILE, SKILLS_SERVER, VIEW_TOOL, SkillStore

_DESCRIPTION = (
    "Load a skill listed in the Skills block. With only name it returns the skill's "
    "instructions; with path (relative to the skill folder, e.g. reference.md) it returns "
    "a file the instructions point to. Read a skill before acting on a task it fits."
)
_TRUNCATED = "\n... [truncated]"


@dataclass(frozen=True)
class SkillTool:
    """The skill tool as the harness sees it (the :class:`MCPTool` attribute shape)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[Mapping[str, Any]], Awaitable[str]] = field(repr=False, compare=False)
    annotations: dict[str, Any] = field(default_factory=dict)
    server: str = SKILLS_SERVER

    @property
    def args_schema(self) -> dict[str, Any]:
        return self.input_schema

    async def invoke(self, args: Mapping[str, Any] | None = None) -> str:
        return await self.run(dict(args or {}))


def skill_tools(store: SkillStore) -> list[SkillTool]:
    """``skill_view`` over ``store``."""

    async def view(args: Mapping[str, Any]) -> str:
        name = str(args.get("name", "") or "").strip()
        skill = store.get(name)
        if skill is None:
            known = ", ".join(each.name for each in store.skills()) or "none"
            raise LookupError(f"Unknown skill {name!r}. Skills: {known}.")
        path = str(args.get("path", "") or "").strip().replace("\\", "/")
        if not path or path == SKILL_FILE:
            limit = store.settings.body_max_chars
            text, truncated = skill.body[:limit], len(skill.body) > limit
            header = f"{skill.name} — {skill.description}"
        else:
            text, truncated = store.read_file(skill.name, path)
            header = f"{skill.name}/{path}"
        lines = text.splitlines()
        start, end = _view_range(args.get("view_range"), len(lines))
        numbered = "\n".join(
            f"{number}\t{line}" for number, line in enumerate(lines[start:end], start=start + 1)
        )
        result = f"{header}\n{numbered}" if numbered else header
        return result + _TRUNCATED if truncated else result

    return [
        SkillTool(
            name=VIEW_TOOL,
            description=_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name from the Skills block."},
                    "path": {
                        "type": "string",
                        "description": "Optional file inside the skill folder; empty returns the instructions.",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Optional [first, last] lines, 1-based, inclusive.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            run=view,
            annotations={"readOnlyHint": True},
        ),
    ]


def _view_range(value: Any, total: int) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return 0, total
    try:
        first, last = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return 0, total
    first = max(1, first)
    last = total if last < 0 else min(total, last)
    return first - 1, max(first - 1, last)


__all__ = ["SkillTool", "skill_tools"]
