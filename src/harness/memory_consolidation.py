"""Opt-in memory consolidation after a successful task (``memory.consolidate_on_goal_end``).

One stateless model call per ``done`` task: the task, the final answer, the rendered action
journal (no raw snapshots), the sites visited, the current index and the full bodies of the
entries for those sites go in; up to ``memory.consolidation_max_entries``
``{path, description, body}`` entries come out. Every entry is written through
:meth:`MemoryStore.create` — the same content policy and size limit as ``memory_write``,
always as ``unverified`` with ``source: agent:<task_id>`` — so staged trust decides whether
later tasks confirm it.

The model sees the current bodies so it can merge instead of replace: an entry it returns is
the whole new file. Rewriting an existing entry keeps its ``uses``/``failures``
(``keep_trust``) — the successes :meth:`MemoryStore.record_outcome` has just counted for it
still hold for the merged body, and resetting them on every task would keep a site's entry
``unverified`` forever.

The session calls this after ``goal_end``, never the engine or ``GoalRunner``: the engine still
never calls a model outside its own turn. A model error, a timeout or an unreadable answer
emits ``memory.consolidation_failed`` and leaves the task result untouched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from typing import Any

from src.agent_loop.prompts import (
    MEMORY_CONSOLIDATION_SYSTEM_PROMPT,
    MEMORY_CONSOLIDATION_USER_PROMPT,
)
from src.contracts import MemoryEntry
from src.harness.memory_store import (
    MemoryEventCallback,
    MemoryPathError,
    MemoryStore,
    MemoryWriteError,
)
from src.messages import system_message, user_message


class MemoryConsolidator:
    """Turns one finished task into at most ``consolidation_max_entries`` unverified entries."""

    def __init__(
        self,
        llm: Any,
        store: MemoryStore,
        *,
        timeout_seconds: float,
        on_event: MemoryEventCallback | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._timeout = timeout_seconds
        self._on_event = on_event

    async def consolidate(
        self,
        *,
        task_id: str,
        task: str,
        final_answer: str,
        action_history: str,
        domains: Iterable[str] = (),
    ) -> list[str]:
        """Write the proposed entries; return their paths. Never raises."""

        settings = self._store.settings
        field_chars = settings.consolidation_field_chars
        reason_chars = settings.event_reason_chars
        visited = sorted({domain for domain in domains if domain})
        prompt = MEMORY_CONSOLIDATION_USER_PROMPT.format(
            task=_clip(task, field_chars) or "(none)",
            final_answer=_clip(final_answer, field_chars) or "(none)",
            domains=", ".join(visited) or "(unknown)",
            action_history=_clip(action_history, field_chars) or "(no tool calls)",
            index=self._store.render_index() or "(empty)",
            entries=self._current_entries(visited) or "(none)",
            max_entries=settings.consolidation_max_entries,
        )
        messages = [system_message(MEMORY_CONSOLIDATION_SYSTEM_PROMPT), user_message(prompt)]
        try:
            response = await asyncio.wait_for(self._llm.complete(messages), self._timeout)
            proposals = _parse(str(getattr(response, "content", response) or ""))
        except Exception as exc:  # noqa: BLE001 - consolidation must never fail the task
            self._emit(
                "memory.consolidation_failed",
                {
                    "task_id": task_id,
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:reason_chars],
                },
            )
            return []

        self._store.bind_task(task_id)
        written: list[str] = []
        rejected: list[dict[str, str]] = []
        for proposal in proposals[: settings.consolidation_max_entries]:
            path = str(proposal.get("path", "") or "")
            try:
                existing = self._store.get(path)
                if existing is not None and existing.status in {"user", "verified"}:
                    # Confirmed knowledge is not demoted by a background guess.
                    raise MemoryWriteError(f"{existing.path} is {existing.status}; left unchanged.")
                entry = self._store.create(
                    path,
                    description=str(proposal.get("description", "") or ""),
                    body=str(proposal.get("body", "") or ""),
                    scope=str(proposal.get("scope", "") or "") or None,
                    keep_trust=True,
                )
            except (MemoryWriteError, MemoryPathError) as exc:
                rejected.append(
                    {"path": path[:reason_chars], "reason": str(exc)[:reason_chars]}
                )
                continue
            written.append(entry.path)
        self._emit(
            "memory.consolidated",
            {"task_id": task_id, "written": written, "rejected": rejected},
        )
        return written

    def _current_entries(self, domains: Sequence[str]) -> str:
        """The full bodies of the entries for ``domains``, so the model can merge into them."""

        seen: dict[str, MemoryEntry] = {}
        for domain in domains:
            for entry in self._store.entries_for_scope(domain):
                seen.setdefault(entry.path, entry)
        limit = self._store.settings.file_max_chars
        return "\n\n".join(
            f"### {entry.path} [{entry.status}] — {entry.description}\n{_clip(entry.body, limit)}"
            for entry in sorted(seen.values(), key=lambda entry: entry.path)
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event_type, payload)


def _parse(content: str) -> Sequence[dict[str, Any]]:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("the consolidation answer has no JSON object")
    data = json.loads(content[start : end + 1])
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("the consolidation answer has no 'entries' list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [truncated]"


__all__ = ["MemoryConsolidator"]
