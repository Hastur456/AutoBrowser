"""Working notes (L2): the acting model's own task-local scratchpad.

With ``memory.working_notes_max_chars > 0`` every tool the model sees offers an optional
:data:`NOTES_ARGUMENT`. It is not a tool argument: :func:`split_notes` strips it at parse time
(like ``approval_request``), the loop keeps the latest value on ``LoopState.working_notes``
and the context renders it as the ``Working Notes`` block on the next turns. The notes are
task-local — not in ``to_session_state`` and reset at the task boundary — and survive history
compaction, the budget and the task digest because they live outside the message history.

Server-neutral: no tool is named, a tool whose own schema already has a ``notes`` property
keeps it (neither offered nor stripped).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from src.contracts import ToolDef, ToolRequest
from src.harness.tools import to_tool_def

#: The optional argument offered to the acting model. A protocol name, not a tunable.
NOTES_ARGUMENT = "notes"

NOTES_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional working notes for the rest of this task: facts found so far (prices, "
        "names, which pages were checked), what is left to do. Replaces your previous notes "
        "and is shown back to you on every following step. Omit it to keep the notes as "
        "they are. Never put element refs here; they expire with the snapshot."
    ),
}


def offer_notes_argument(tools: Sequence[Any]) -> tuple[list[ToolDef], frozenset[str]]:
    """Model-visible defs with :data:`NOTES_ARGUMENT` on every tool.

    Returns the defs and the names of the tools whose own schema already has that property.
    """

    defs: list[ToolDef] = []
    colliding: set[str] = set()
    for tool in tools:
        tool_def = to_tool_def(tool)
        schema = copy.deepcopy(dict(tool_def.input_schema or {}))
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and NOTES_ARGUMENT in properties:
            colliding.add(tool_def.name)
            defs.append(tool_def)
            continue
        if schema.get("type", "object") != "object":
            defs.append(tool_def)
            continue
        schema["type"] = "object"
        schema["properties"] = {
            **dict(properties or {}),
            NOTES_ARGUMENT: dict(NOTES_ARGUMENT_SCHEMA),
        }
        defs.append(
            ToolDef(name=tool_def.name, description=tool_def.description, input_schema=schema)
        )
    return defs, frozenset(colliding)


def split_notes(
    request: Mapping[str, Any],
    colliding: frozenset[str] = frozenset(),
    max_chars: int = 0,
) -> tuple[ToolRequest, str | None]:
    """Remove :data:`NOTES_ARGUMENT` from ``request``; return it cut to ``max_chars``.

    ``None`` means the model did not write notes this turn (keep the previous ones).
    """

    cleaned: dict[str, Any] = dict(request)
    name = str(cleaned.get("name", "") or "")
    args = cleaned.get("args")
    if name in colliding or not isinstance(args, Mapping) or NOTES_ARGUMENT not in args:
        return cleaned, None  # type: ignore[return-value]
    remaining = dict(args)
    value = remaining.pop(NOTES_ARGUMENT)
    cleaned["args"] = remaining
    text = "" if value is None else str(value).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + " [truncated]"
    return cleaned, text  # type: ignore[return-value]


__all__ = [
    "NOTES_ARGUMENT",
    "NOTES_ARGUMENT_SCHEMA",
    "offer_notes_argument",
    "split_notes",
]
