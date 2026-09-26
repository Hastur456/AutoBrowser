"""Server-neutral action journal: what the agent tried and what each attempt returned.

The journal is how the loop tells a model "you already did exactly this and got exactly
that" without knowing anything about the tools involved. Every executed tool call becomes an
:class:`ActionRecord` keyed by

- ``args_key`` — a hash of the tool name plus canonical-JSON arguments (the *call*), and
- ``outcome_key`` — a hash of the status plus whitespace-normalized result/error text
  (the *outcome*).

``occurrence`` counts how many times this call has produced this outcome in the current
task, so a repeat is detected whether the calls are adjacent (``A, A``) or interleaved with
other calls (``A, B, A, B``). No tool names, result formats (``[]``, element refs, error
texts) or servers are special-cased.

This module is a pure leaf: it imports nothing from the loop, the harness or the browser
package, and every budget is passed in by the caller (read from ``src.config`` there).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionRecord:
    """One executed tool call and a fingerprint of its outcome."""

    tool: str
    args: str
    args_key: str
    outcome_key: str
    status: str
    summary: str
    occurrence: int = 1


def _canonical_args(args: Any) -> str:
    try:
        return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(args)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def call_key(tool: Any, args: Any) -> str:
    """Stable identity of a call: tool name plus canonical arguments."""

    return _digest(f"{tool or ''}\n{_canonical_args(args)}")


def outcome_key(status: Any, content: Any, error: Any) -> str:
    """Stable identity of an outcome: status plus normalized result/error text."""

    return _digest(f"{status or ''}\n{_normalized_text(content)}\n{_normalized_text(error)}")


def record_action(
    history: Sequence[ActionRecord],
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    preview_chars: int,
) -> ActionRecord:
    """Build the record for one executed call, counting identical earlier outcomes."""

    # The request name is what the model asked for and what the guard compares against.
    tool = str(request.get("name") or result.get("name") or "")
    args = request.get("args") or {}
    status = str(result.get("status", "") or "")
    content = result.get("content", "")
    error = result.get("error", "")
    record_call = call_key(tool, args)
    record_outcome = outcome_key(status, content, error)
    prior = sum(
        1
        for item in history
        if item.args_key == record_call and item.outcome_key == record_outcome
    )
    payload = _normalized_text(content) or _normalized_text(error) or "(empty output)"
    return ActionRecord(
        tool=tool,
        args=_preview(_canonical_args(args), preview_chars),
        args_key=record_call,
        outcome_key=record_outcome,
        status=status,
        summary=_preview(payload, preview_chars),
        occurrence=prior + 1,
    )


def identical_outcome_count(
    history: Sequence[ActionRecord],
    tool: Any,
    args: Any,
) -> int:
    """How many times the latest outcome of this exact call has already been returned.

    ``0`` means the call was never executed in this task.
    """

    key = call_key(tool, args)
    for item in reversed(history):
        if item.args_key == key:
            return item.occurrence
    return 0


def repeat_note(record: ActionRecord) -> str:
    """Model-facing note for a call that reproduced an earlier outcome, else ``""``."""

    if record.occurrence < 2:
        return ""
    return (
        f"Repeat detected: {record.tool} with these exact arguments has now returned this "
        f"identical result {record.occurrence} times in this task. Repeating it will not "
        "produce new information; the assumption behind this call does not hold for the "
        "current state."
    )


def render_action_history(history: Sequence[ActionRecord], limit: int) -> str:
    """Compact numbered journal of the last ``limit`` calls for the model context."""

    if not history or limit <= 0:
        return ""
    start = max(0, len(history) - limit)
    lines = []
    if start:
        lines.append(f"({start} earlier calls omitted)")
    for index, item in enumerate(history[start:], start=start + 1):
        line = f"{index}. {item.tool} {item.args} -> {item.status}: {item.summary}"
        if item.occurrence > 1:
            line += f" [identical result {item.occurrence}x]"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "ActionRecord",
    "call_key",
    "identical_outcome_count",
    "outcome_key",
    "record_action",
    "render_action_history",
    "repeat_note",
]
