"""Load, summarize, and replay durable AutoBrowser JSONL traces."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent_loop.events import EventRecord


@dataclass(frozen=True)
class TraceAction:
    """One proposed tool action in a replayable trace."""

    index: int
    name: str
    args: dict[str, Any]
    status: str | None = None


@dataclass(frozen=True)
class TraceSummary:
    """Compact metrics extracted from an event trace."""

    terminal_status: str
    task: str
    final_answer: str
    model_turn_count: int
    tool_call_count: int
    policy_block_count: int
    repeated_action_count: int
    trace_complete: bool


def load_events(path: Path) -> list[EventRecord]:
    """Load typed event records from a JSONL trace file."""

    records: list[EventRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(EventRecord.from_json_dict(json.loads(line)))
    return records


def iter_action_sequence(events: list[EventRecord]) -> Iterator[TraceAction]:
    """Yield proposed actions with optional execution status."""

    statuses = _tool_statuses(events)
    index = 0
    for event in events:
        if event.type != "action.proposed":
            continue
        request = event.payload.get("tool_request") or {}
        if not isinstance(request, dict):
            continue
        name = str(request.get("name", "") or "")
        if not name:
            continue
        index += 1
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        yield TraceAction(
            index=index,
            name=name,
            args=dict(args),
            status=statuses.pop(0) if statuses else None,
        )


def summarize_trace(events: list[EventRecord]) -> TraceSummary:
    """Summarize lifecycle and action metrics for a trace."""

    terminal = _terminal_event(events)
    actions = list(iter_action_sequence(events))
    return TraceSummary(
        terminal_status=_terminal_status(terminal),
        task=_first_task(events),
        final_answer=_final_answer(terminal),
        model_turn_count=sum(1 for event in events if event.type == "model.responded"),
        tool_call_count=sum(1 for event in events if event.type == "tool.finished"),
        policy_block_count=_policy_block_count(events),
        repeated_action_count=_repeated_action_count(actions),
        trace_complete=bool(
            any(event.type == "goal.started" for event in events) and terminal is not None
        ),
    )


def print_action_sequence(events: list[EventRecord]) -> str:
    """Render a compact action sequence for CLI output and eval failures."""

    lines: list[str] = []
    task = _first_task(events)
    if task:
        lines.append(f"goal.started: {task}")
    for action in iter_action_sequence(events):
        suffix = f" -> {action.status}" if action.status else ""
        lines.append(f"{action.index}. {action.name} {json.dumps(action.args, ensure_ascii=False)}{suffix}")
    terminal = _terminal_event(events)
    if terminal is not None:
        status = _terminal_status(terminal)
        final_answer = _final_answer(terminal)
        line = f"goal.{status}"
        if final_answer:
            line = f"{line}: {final_answer}"
        lines.append(line)
    return "\n".join(lines)


def _tool_statuses(events: list[EventRecord]) -> list[str]:
    statuses: list[str] = []
    for event in events:
        if event.type != "tool.finished":
            continue
        result = event.payload.get("tool_result") or {}
        if isinstance(result, dict):
            statuses.append(str(result.get("status", "") or ""))
    return statuses


def _terminal_event(events: list[EventRecord]) -> EventRecord | None:
    for event in reversed(events):
        if event.type in {
            "goal.completed",
            "goal.blocked",
            "goal.failed",
            "goal.cancelled",
        }:
            return event
    return None


def _terminal_status(event: EventRecord | None) -> str:
    if event is None:
        return "missing"
    return event.type.removeprefix("goal.")


def _first_task(events: list[EventRecord]) -> str:
    for event in events:
        task = event.payload.get("task")
        if task:
            return str(task)
    return ""


def _final_answer(event: EventRecord | None) -> str:
    if event is None:
        return ""
    result = event.payload.get("result")
    if isinstance(result, dict):
        final_answer = result.get("final_answer")
        if final_answer:
            return str(final_answer)
        for value in result.values():
            if isinstance(value, dict) and value.get("final_answer"):
                return str(value["final_answer"])
    error = event.payload.get("error")
    return str(error) if error else ""


def _policy_block_count(events: list[EventRecord]) -> int:
    count = 0
    for event in events:
        if event.type != "policy.decided":
            continue
        if event.payload.get("decision") == "blocked":
            count += 1
    return count


def _repeated_action_count(actions: list[TraceAction]) -> int:
    count = 0
    previous: tuple[str, str] | None = None
    for action in actions:
        current = (action.name, json.dumps(action.args, sort_keys=True, default=str))
        if current == previous:
            count += 1
        previous = current
    return count


__all__ = [
    "TraceAction",
    "TraceSummary",
    "iter_action_sequence",
    "load_events",
    "print_action_sequence",
    "summarize_trace",
]
