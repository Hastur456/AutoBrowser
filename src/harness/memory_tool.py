"""``memory_view`` / ``memory_write``: persistent memory as two harness-native tools.

Shaped like :class:`~src.harness.mcp_tools.MCPTool` (``name``, ``description``,
``input_schema``, ``server``, ``annotations``, async ``invoke``), so the tool broker, the
permission engine (``check.server == "memory"``), ``tool_is_read_only`` and the model tool
schemas handle them without any special case. The engine never names them.

* ``memory_view`` — ``readOnlyHint``: the index (no ``path``) or one entry, optionally a
  line range. Allowed in ``read_only`` permission mode.
* ``memory_write`` — ``create`` / ``str_replace`` / ``delete`` on agent entries. Narrower than
  the Anthropic memory tool on purpose: no ``insert``/``rename``, and the model never sees or
  edits the frontmatter — the store stamps ``status: unverified`` and
  ``source: agent:<task_id>``. Every write passes the store's ``MemoryContentPolicy``.

A refused write raises (the broker turns it into a tool error the model reads; the turn is
not terminal). Human review of writes is a user permission rule, never a built-in one::

    permissions:
      rules:
        - {id: memory-write-review, decision: ask, server: memory, tool: memory_write}
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.harness.memory_store import MemoryStore, MemoryWriteError

MEMORY_SERVER = "memory"
VIEW_TOOL = "memory_view"
WRITE_TOOL = "memory_write"

_VIEW_DESCRIPTION = (
    "Read persistent memory saved by earlier sessions. Without a path it lists every entry "
    "(path, trust status, description); with a path such as sites/example.com.md it returns "
    "that entry. Memory holds hints; the current page snapshot always wins."
)
_WRITE_DESCRIPTION = (
    "Save what a later task would need into persistent memory. Commands: create (path, "
    "description, body; overwrites your own entry), str_replace (path, old_str, new_str), "
    "delete (path). Paths: sites/<domain>.md for one site, procedures/<name>.md for a "
    "procedure. Save URL templates, the visible names of controls and the steps that worked. "
    "Never save element refs, CSS/XPath selectors, form values, passwords or personal data. "
    "New entries are unverified until later tasks succeed with them; entries written by the "
    "user cannot be changed."
)


@dataclass(frozen=True)
class MemoryTool:
    """One memory tool as the harness sees it (the :class:`MCPTool` attribute shape)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[Mapping[str, Any]], Awaitable[str]] = field(repr=False, compare=False)
    annotations: dict[str, Any] = field(default_factory=dict)
    server: str = MEMORY_SERVER

    @property
    def args_schema(self) -> dict[str, Any]:
        return self.input_schema

    async def invoke(self, args: Mapping[str, Any] | None = None) -> str:
        return await self.run(dict(args or {}))


def memory_tools(store: MemoryStore) -> list[MemoryTool]:
    """``memory_view`` and ``memory_write`` over ``store``."""

    async def view(args: Mapping[str, Any]) -> str:
        path = str(args.get("path", "") or "").strip()
        if not path:
            index = store.render_index()
            return index or "Persistent memory is empty."
        entry = store.get(path)
        if entry is None:
            raise LookupError(f"{path} does not exist. Call memory_view without a path to list entries.")
        lines = entry.body.splitlines()
        start, end = _view_range(args.get("view_range"), len(lines))
        numbered = "\n".join(
            f"{number}\t{line}" for number, line in enumerate(lines[start:end], start=start + 1)
        )
        header = f"{entry.path} [{entry.status}] scope={entry.scope} — {entry.description}"
        text = f"{header}\n{numbered}" if numbered else header
        limit = store.settings.file_max_chars
        return text if len(text) <= limit else text[:limit].rstrip() + "\n... [truncated]"

    async def write(args: Mapping[str, Any]) -> str:
        command = str(args.get("command", "") or "").strip()
        path = str(args.get("path", "") or "").strip()
        if command == "create":
            entry = store.create(
                path,
                description=str(args.get("description", "") or ""),
                body=str(args.get("body", "") or ""),
                scope=str(args.get("scope", "") or "") or None,
            )
            return f"Saved {entry.path} (scope {entry.scope}, unverified)."
        if command == "str_replace":
            entry = store.str_replace(
                path,
                str(args.get("old_str", "") or ""),
                str(args.get("new_str", "") or ""),
            )
            return f"Updated {entry.path} (unverified)."
        if command == "delete":
            store.delete(path)
            return f"Deleted {path}."
        raise MemoryWriteError("command must be one of: create, str_replace, delete.")

    return [
        MemoryTool(
            name=VIEW_TOOL,
            description=_VIEW_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Entry path; empty lists every entry.",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Optional [first, last] body lines, 1-based, inclusive.",
                    },
                },
                "additionalProperties": False,
            },
            run=view,
            annotations={"readOnlyHint": True},
        ),
        MemoryTool(
            name=WRITE_TOOL,
            description=_WRITE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "enum": ["create", "str_replace", "delete"]},
                    "path": {
                        "type": "string",
                        "description": "sites/<domain>.md or procedures/<name>.md",
                    },
                    "description": {
                        "type": "string",
                        "description": "create: one line for the memory index.",
                    },
                    "body": {"type": "string", "description": "create: the markdown body."},
                    "scope": {
                        "type": "string",
                        "description": "create: the site domain (defaults to the file name for sites/).",
                    },
                    "old_str": {"type": "string", "description": "str_replace: exact text to replace."},
                    "new_str": {"type": "string", "description": "str_replace: replacement text."},
                },
                "required": ["command", "path"],
                "additionalProperties": False,
            },
            run=write,
            annotations={},
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


__all__ = ["MEMORY_SERVER", "VIEW_TOOL", "WRITE_TOOL", "MemoryTool", "memory_tools"]
