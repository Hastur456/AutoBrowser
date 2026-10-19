"""Project redacted event records into :class:`FeedItem`s and sanitize displayed text.

The projection decides by the event ``type`` and generic payload fields only; a tool name is
copied into the item to be shown, never compared. Page URLs are read through the browser
layer's text-format helper (``- Page URL:``), not by tool.

Input records are ``EventRecord.to_json_dict()`` mappings, so secrets are already redacted
by :func:`src.agent_loop.events.redact_json_safe`. Every string that reaches a feed item is
also passed through :func:`sanitize_text` (§6.1, B9 of the terminal UI design): page content
must never inject control sequences into the terminal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src.browser.pages import page_url
from src.ui.views import FeedItem, FeedKind

#: C0 controls except ``\t`` and ``\n``, DEL, C1 controls, and the BiDi embedding,
#: override and isolate characters. ``\r`` is dropped too: it rewinds the cursor.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
#: A whole escape sequence (CSI, OSC, DCS/APC/PM/SOS strings, two-character escapes), so
#: its parameters do not survive as stray text once ``ESC`` itself is removed.
_ESCAPE_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|[@-Z\\-_])"
)

_GOAL_EVENTS: dict[str, str] = {
    "goal.completed": "completed",
    "goal.blocked": "blocked",
    "goal.cancelled": "cancelled",
    "goal.failed": "failed",
}

_KIND_BY_TYPE: dict[str, FeedKind] = {
    "goal.started": "goal_started",
    "plan.updated": "plan",
    "turn.started": "turn",
    "model.requested": "model",
    "model.responded": "model",
    "action.proposed": "call",
    "policy.decided": "gate",
    "hook.decided": "gate",
    "permission.decided": "gate",
    "approval.requested": "approval_requested",
    "approval.resolved": "approval_resolved",
    "approval.answered": "approval_resolved",
    "tool.started": "tool_started",
    "tool.finished": "tool_finished",
    "observation.compiled": "observation",
}


def sanitize_text(value: object) -> str:
    """``value`` as text without terminal control sequences (keeps ``\\n`` and ``\\t``)."""

    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n")
    text = _ESCAPE_SEQUENCE.sub("", text)
    return _UNSAFE_CHARS.sub("", text)


def project_event(record: Mapping[str, Any]) -> FeedItem | None:
    """The feed item of one redacted event record, or ``None`` for events a feed ignores.

    ``model.responded`` and allowing gates carry nothing to show; the session-level
    ``session.*`` events belong to the status line, not the feed.
    """

    event_type = str(record.get("type") or "")
    payload = record.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    base = {
        "event_type": event_type,
        "sequence": _int(record.get("sequence")) or 0,
        "task_id": sanitize_text(record.get("task_id") or ""),
        "timestamp": _timestamp(record.get("timestamp")),
    }

    if event_type in _GOAL_EVENTS:
        status = _GOAL_EVENTS[event_type]
        answer = payload.get("final_answer")
        if not answer:
            # Records written before goal.* carried structured fields nest it in ``result``.
            result = payload.get("result")
            answer = result.get("final_answer") if isinstance(result, Mapping) else ""
        reason = payload.get("reason") or _error_text(payload.get("error"))
        return FeedItem(
            kind="outcome",
            status=status,
            text=sanitize_text(answer or reason or ""),
            turns=_int(payload.get("turns")),
            task=sanitize_text(payload.get("task") or ""),
            **base,
        )
    if event_type.startswith("memory."):
        return FeedItem(
            kind="memory",
            status=event_type.removeprefix("memory."),
            text=sanitize_text(payload.get("reason") or payload.get("path") or ""),
            **base,
        )

    kind = _KIND_BY_TYPE.get(event_type)
    if kind is None:
        if event_type.startswith("session.") or event_type.startswith("task."):
            return None
        return FeedItem(kind="other", **base)
    if kind == "goal_started":
        return FeedItem(kind=kind, task=sanitize_text(payload.get("task") or ""), **base)
    if kind == "plan":
        steps = _steps(payload.get("steps"))
        return FeedItem(
            kind=kind,
            steps=tuple(text for text, _ in steps),
            step_statuses=tuple(status for _, status in steps),
            current_step=_int(payload.get("current_step")),
            replan_count=_int(payload.get("replan_count")),
            **base,
        )
    if kind == "turn":
        return FeedItem(
            kind=kind,
            turn=_int(payload.get("turn")),
            current_step=_int(payload.get("current_step")),
            **base,
        )
    if kind == "model":
        if event_type == "model.responded":
            return None
        return FeedItem(kind=kind, phase=sanitize_text(payload.get("phase") or ""), **base)
    if kind == "call":
        request = _mapping(payload.get("tool_request"))
        return FeedItem(
            kind=kind,
            tool=sanitize_text(request.get("name") or ""),
            args=_sanitize_args(request.get("args")),
            text=sanitize_text(request.get("reason") or ""),
            **base,
        )
    if kind == "gate":
        return _gate_item(event_type, payload, base)
    if kind == "approval_requested":
        request = _mapping(payload.get("tool_request"))
        return FeedItem(
            kind=kind,
            tool=sanitize_text(request.get("name") or ""),
            args=_sanitize_args(request.get("args")),
            text=sanitize_text(payload.get("reason") or ""),
            rule=sanitize_text(payload.get("rule_id") or ""),
            **base,
        )
    if kind == "approval_resolved":
        decision = payload.get("decision") or payload.get("answer") or ""
        return FeedItem(
            kind=kind,
            decision=sanitize_text(decision),
            origin=sanitize_text(payload.get("by") or payload.get("via") or ""),
            status=sanitize_text(payload.get("scope") or ""),
            **base,
        )
    if kind == "tool_started":
        request = _mapping(payload.get("tool_request"))
        return FeedItem(kind=kind, tool=sanitize_text(request.get("name") or ""), **base)
    if kind == "tool_finished":
        result = _mapping(payload.get("tool_result"))
        content = result.get("content") or ""
        return FeedItem(
            kind=kind,
            tool=sanitize_text(result.get("name") or ""),
            status=sanitize_text(result.get("status") or ""),
            text=sanitize_text(result.get("error") or content),
            url=sanitize_text(page_url(content)),
            **base,
        )
    # observation.compiled
    return FeedItem(
        kind="observation",
        text=sanitize_text(payload.get("observation") or ""),
        status="snapshot" if payload.get("has_snapshot") else "",
        **base,
    )


def _gate_item(event_type: str, payload: Mapping[str, Any], base: dict[str, Any]) -> FeedItem | None:
    """A hook, permission or progress-guard decision; allowing ones are not shown."""

    if event_type == "policy.decided":
        decision = str(payload.get("decision") or "")
        if decision != "blocked":
            return None
        return FeedItem(
            kind="gate",
            decision="blocked",
            origin="guard",
            text=sanitize_text(payload.get("reason") or ""),
            rule=sanitize_text(payload.get("rule_id") or ""),
            **base,
        )
    decision = str(payload.get("decision") or "")
    if event_type == "permission.decided":
        return FeedItem(
            kind="gate",
            decision=sanitize_text(decision),
            origin="permission",
            tool=sanitize_text(payload.get("tool") or ""),
            server=sanitize_text(payload.get("server") or ""),
            text=sanitize_text(payload.get("reason") or ""),
            rule=sanitize_text(payload.get("rule_id") or payload.get("source") or ""),
            **base,
        )
    # hook.decided: only a deny/ask (or a failed hook) is worth a line.
    error = payload.get("error")
    if decision not in {"deny", "ask"} and not error:
        return None
    return FeedItem(
        kind="gate",
        decision=sanitize_text(decision or "error"),
        origin="hook",
        tool=sanitize_text(payload.get("tool") or ""),
        server=sanitize_text(payload.get("server") or ""),
        text=sanitize_text(payload.get("reason") or error or ""),
        rule=sanitize_text(payload.get("hook_id") or ""),
        **base,
    )


def _sanitize_args(value: Any) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {sanitize_text(key): _sanitize_value(item) for key, item in value.items()}


def _sanitize_value(value: Any) -> object:
    if isinstance(value, Mapping):
        return {sanitize_text(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(value)


def _steps(value: Any) -> tuple[tuple[str, str], ...]:
    """Plan steps as ``(text, status)``; a step is a string or a ``PlanStep`` mapping."""

    if not isinstance(value, (list, tuple)):
        return ()
    steps: list[tuple[str, str]] = []
    for step in value:
        status = ""
        if isinstance(step, Mapping):
            status = sanitize_text(step.get("status") or "")
            step = step.get("description") or step.get("text") or step.get("title") or ""
        text = sanitize_text(step).strip()
        if text:
            steps.append((text, status))
    return tuple(steps)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _error_text(value: Any) -> str:
    """A redacted exception is ``{"type", "message"}``; show it as ``Type: message``."""

    if isinstance(value, Mapping):
        kind = str(value.get("type") or "")
        message = str(value.get("message") or "")
        return f"{kind}: {message}" if kind and message else kind or message
    return "" if value is None else str(value)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = ["project_event", "sanitize_text"]
