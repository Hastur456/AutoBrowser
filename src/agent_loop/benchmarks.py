"""Timings and usage of live runs, derived from their event traces.

Every figure comes from events the engine and the session already write: ``goal.started`` and
the terminal goal event, ``model.requested``/``model.responded`` pairs (with the provider's
``usage``), ``tool.started``/``tool.finished`` pairs. Nothing here runs a model or a browser.

:func:`extract_task_timings` is the per-task contract of §4.5.1/§4.7 of
docs/development/2026-10-19-autonomous-test-pipeline-design.md; :func:`summarize_run` folds
the tasks of one session (a Golden Set scenario) into the figures of a batch report. The
statistics, A/B comparison and history of that design build on these later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.agent_loop.events import EventRecord
from src.agent_loop.metrics import extract_event_metrics

_TERMINAL_EVENTS = {"goal.completed", "goal.failed", "goal.blocked", "goal.cancelled"}


@dataclass(frozen=True)
class TaskTimings:
    """Timings and usage of one task (one goal)."""

    task_id: str
    #: ``goal.started`` → the terminal goal event.
    task_latency_ms: int | None
    #: ``goal.started`` → the first ``model.responded`` of the ``plan`` phase.
    time_to_plan_ms: int | None
    #: ``goal.started`` → the first ``tool.started``.
    time_to_first_tool_ms: int | None
    #: Phase (``plan``, ``agent``, ``approval``) → latency of each model call.
    model_latency_ms: Mapping[str, tuple[int, ...]]
    #: Cold model loads the provider reported (``usage.load_duration_ms``).
    model_load_ms: tuple[int, ...]
    #: Latency of each tool call.
    tool_latency_ms: tuple[int, ...]
    model_calls: int
    #: Sums over the calls that reported them; ``None`` when no call did.
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class RunMetrics:
    """The figures of one session run (every task of a Golden Set scenario)."""

    tasks: int
    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    #: Summed latency of every model call, and of every tool call.
    model_ms: int
    tool_calls: int
    tool_ms: int
    tool_errors: int
    policy_blocks: int
    #: Of the first task: how long until the agent touched a tool.
    time_to_first_tool_ms: int | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_ms": self.model_ms,
            "tool_calls": self.tool_calls,
            "tool_ms": self.tool_ms,
            "tool_errors": self.tool_errors,
            "policy_blocks": self.policy_blocks,
            "time_to_first_tool_ms": self.time_to_first_tool_ms,
        }


def split_by_task(events: Iterable[EventRecord]) -> list[list[EventRecord]]:
    """The events of each task, in the order the tasks started; session events are dropped."""

    tasks: dict[str, list[EventRecord]] = {}
    for event in events:
        if event.task_id:
            tasks.setdefault(event.task_id, []).append(event)
    return list(tasks.values())


def extract_task_timings(events: Sequence[EventRecord]) -> TaskTimings:
    """Timings and usage of one task's events (see :class:`TaskTimings`)."""

    started = _first_time(events, lambda event: event.type == "goal.started")
    terminal = _first_time(events, lambda event: event.type in _TERMINAL_EVENTS)
    plan = _first_time(
        events,
        lambda event: event.type == "model.responded" and event.payload.get("phase") == "plan",
    )
    first_tool = _first_time(events, lambda event: event.type == "tool.started")

    model_latency: dict[str, list[int]] = {}
    pending_model: dict[str, datetime] = {}
    pending_tools: list[datetime] = []
    tool_latency: list[int] = []
    loads: list[int] = []
    model_calls = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    for event in events:
        phase = str(event.payload.get("phase", "") or "")
        if event.type == "model.requested":
            pending_model[phase] = event.timestamp
        elif event.type == "model.responded":
            model_calls += 1
            requested = pending_model.pop(phase, None)
            if requested is not None:
                model_latency.setdefault(phase, []).append(_ms(requested, event.timestamp))
            usage = event.payload.get("usage")
            if isinstance(usage, Mapping):
                input_tokens = _add(input_tokens, usage.get("input_tokens"))
                output_tokens = _add(output_tokens, usage.get("output_tokens"))
                load = usage.get("load_duration_ms")
                if _is_count(load) and load > 0:
                    loads.append(load)
        elif event.type == "tool.started":
            pending_tools.append(event.timestamp)
        elif event.type == "tool.finished" and pending_tools:
            tool_latency.append(_ms(pending_tools.pop(0), event.timestamp))

    return TaskTimings(
        task_id=str(events[0].task_id or "") if events else "",
        task_latency_ms=_span(started, terminal),
        time_to_plan_ms=_span(started, plan),
        time_to_first_tool_ms=_span(started, first_tool),
        model_latency_ms={phase: tuple(values) for phase, values in model_latency.items()},
        model_load_ms=tuple(loads),
        tool_latency_ms=tuple(tool_latency),
        model_calls=model_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def summarize_run(events: Iterable[EventRecord]) -> RunMetrics:
    """Fold every task of a session's events into one :class:`RunMetrics`."""

    tasks = split_by_task(events)
    timings = [extract_task_timings(task) for task in tasks]
    counts = [extract_event_metrics(task) for task in tasks]
    input_tokens: int | None = None
    output_tokens: int | None = None
    for timing in timings:
        input_tokens = _add(input_tokens, timing.input_tokens)
        output_tokens = _add(output_tokens, timing.output_tokens)
    return RunMetrics(
        tasks=len(tasks),
        model_calls=sum(timing.model_calls for timing in timings),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_ms=sum(sum(values) for timing in timings for values in timing.model_latency_ms.values()),
        tool_calls=sum(len(timing.tool_latency_ms) for timing in timings),
        tool_ms=sum(sum(timing.tool_latency_ms) for timing in timings),
        tool_errors=sum(1 for task in tasks for event in task if _is_tool_error(event)),
        policy_blocks=sum(count.policy_block_count for count in counts),
        time_to_first_tool_ms=timings[0].time_to_first_tool_ms if timings else None,
    )


def _is_tool_error(event: EventRecord) -> bool:
    """A tool call that finished with an error (a failed goal is not a tool error)."""

    if event.type != "tool.finished":
        return False
    result = event.payload.get("tool_result")
    if not isinstance(result, Mapping):
        return False
    status = str(result.get("status") or "").lower()
    return bool(result.get("error")) or status in {"error", "failed", "failure"}


def _first_time(events: Sequence[EventRecord], matches: Any) -> datetime | None:
    return next((event.timestamp for event in events if matches(event)), None)


def _span(start: datetime | None, end: datetime | None) -> int | None:
    return _ms(start, end) if start is not None and end is not None else None


def _ms(start: datetime, end: datetime) -> int:
    return max(int((end - start).total_seconds() * 1000), 0)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _add(total: int | None, value: Any) -> int | None:
    if not _is_count(value):
        return total
    return (total or 0) + value


__all__ = [
    "RunMetrics",
    "TaskTimings",
    "extract_task_timings",
    "split_by_task",
    "summarize_run",
]
