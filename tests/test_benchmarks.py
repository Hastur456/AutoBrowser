"""Timings and usage derived from synthetic event traces."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent_loop.benchmarks import extract_task_timings, split_by_task, summarize_run
from src.agent_loop.events import EventRecord

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _event(seconds: float, event_type: str, task_id: str | None = "t1", **payload: Any) -> EventRecord:
    return EventRecord(
        type=event_type,  # type: ignore[arg-type]
        source="test",
        payload=payload,
        timestamp=T0 + timedelta(seconds=seconds),
        task_id=task_id,
    )


def _task(task_id: str = "t1", offset: float = 0.0) -> list[EventRecord]:
    def at(seconds: float, event_type: str, **payload: Any) -> EventRecord:
        return _event(offset + seconds, event_type, task_id, **payload)

    return [
        at(0.0, "goal.started", task="Open the page."),
        at(0.1, "model.requested", phase="plan"),
        at(1.1, "model.responded", phase="plan", usage={"input_tokens": 100, "output_tokens": 10, "load_duration_ms": 500}),
        at(1.2, "model.requested", phase="agent"),
        at(2.2, "model.responded", phase="agent", usage={"input_tokens": 200, "output_tokens": "[REDACTED]"}),
        at(2.3, "tool.started", tool_request={"name": "open"}),
        at(2.8, "tool.finished", tool_result={"name": "open", "status": "error", "error": "boom"}),
        at(2.9, "permission.decided", decision="deny", tool="click"),
        at(3.0, "model.requested", phase="agent"),
        at(3.5, "model.responded", phase="agent"),
        at(4.0, "goal.completed", status="completed", final_answer="Done."),
    ]


def test_task_timings_pair_the_events_of_one_task() -> None:
    timings = extract_task_timings(_task())

    assert timings.task_id == "t1"
    assert timings.task_latency_ms == 4000
    assert timings.time_to_plan_ms == 1100
    assert timings.time_to_first_tool_ms == 2300
    assert timings.model_latency_ms == {"plan": (1000,), "agent": (1000, 500)}
    assert timings.model_load_ms == (500,)
    assert timings.tool_latency_ms == (500,)
    assert timings.model_calls == 3


def test_tokens_sum_the_reported_counts_only() -> None:
    timings = extract_task_timings(_task())

    assert timings.input_tokens == 300
    assert timings.output_tokens == 10  # the redacted value is not a count


def test_tokens_are_none_when_no_call_reports_them() -> None:
    events = [
        _event(0, "goal.started"),
        _event(0.1, "model.requested", phase="plan"),
        _event(0.5, "model.responded", phase="plan"),
        _event(1, "goal.blocked"),
    ]

    timings = extract_task_timings(events)

    assert (timings.input_tokens, timings.output_tokens) == (None, None)
    assert timings.time_to_first_tool_ms is None
    assert timings.task_latency_ms == 1000


def test_split_by_task_keeps_task_order_and_drops_session_events() -> None:
    events = [_event(0, "session.started", None), *_task("a"), *_task("b", 10), _event(20, "session.closed", None)]

    tasks = split_by_task(events)

    assert [task[0].task_id for task in tasks] == ["a", "b"]
    assert all(event.task_id for task in tasks for event in task)


def test_summarize_run_folds_every_task() -> None:
    events = [_event(0, "session.started", None), *_task("a"), *_task("b", 10)]

    metrics = summarize_run(events).to_json_dict()

    assert metrics == {
        "tasks": 2,
        "model_calls": 6,
        "input_tokens": 600,
        "output_tokens": 20,
        "model_ms": 5000,
        "tool_calls": 2,
        "tool_ms": 1000,
        "tool_errors": 2,
        "policy_blocks": 2,
        "time_to_first_tool_ms": 2300,
    }


def test_an_empty_trace_has_no_tasks() -> None:
    metrics = summarize_run([]).to_json_dict()

    assert metrics["tasks"] == 0
    assert metrics["input_tokens"] is None
    assert metrics["time_to_first_tool_ms"] is None
