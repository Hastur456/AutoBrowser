from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent_loop.events import EventRecord
from src.agent_loop.metrics import EventMetrics, extract_event_metrics


def test_extract_event_metrics_for_completed_trace() -> None:
    start = datetime(2026, 7, 30, tzinfo=UTC)
    events = [
        _event("goal.started", start, {"task": "inspect page"}),
        _event("model.responded", start + timedelta(seconds=1)),
        _event("action.proposed", start + timedelta(seconds=2)),
        _event(
            "policy.decided",
            start + timedelta(seconds=3),
            {"decision": "allowed"},
        ),
        _event(
            "tool.finished",
            start + timedelta(seconds=4),
            {"tool_result": {"name": "browser_snapshot", "status": "success"}},
        ),
        _event("observation.compiled", start + timedelta(seconds=5)),
        _event("approval.requested", start + timedelta(seconds=6)),
        _event(
            "goal.completed",
            start + timedelta(seconds=7, milliseconds=250),
            {"result": {"agent": {"final_answer": "done"}}},
        ),
    ]

    metrics = extract_event_metrics(events)

    assert metrics == EventMetrics(
        duration_ms=7250,
        terminal_status="completed",
        model_turn_count=1,
        tool_call_count=1,
        policy_block_count=0,
        approval_request_count=1,
        observation_count=1,
        error_count=0,
        final_answer="done",
    )
    assert metrics.to_dict()["duration_ms"] == 7250


def test_extract_event_metrics_for_failed_trace_counts_errors() -> None:
    start = datetime(2026, 7, 30, tzinfo=UTC)
    events = [
        _event("goal.started", start),
        _event("model.responded", start + timedelta(seconds=1)),
        _event(
            "policy.decided",
            start + timedelta(seconds=2),
            {"decision": "blocked"},
        ),
        _event(
            "tool.finished",
            start + timedelta(seconds=3),
            {"tool_result": {"status": "error", "error": "invalid ref"}},
        ),
        _event("graph.failed", start + timedelta(seconds=4), {"error": "boom"}),
        _event("goal.failed", start + timedelta(seconds=5), {"error": "boom"}),
    ]

    metrics = extract_event_metrics(events)

    assert metrics.duration_ms == 5000
    assert metrics.terminal_status == "failed"
    assert metrics.model_turn_count == 1
    assert metrics.tool_call_count == 1
    assert metrics.policy_block_count == 1
    assert metrics.approval_request_count == 0
    assert metrics.observation_count == 0
    assert metrics.error_count == 3
    assert metrics.final_answer == ""


def test_extract_event_metrics_for_incomplete_trace_accepts_dict_events() -> None:
    events: list[dict[str, Any]] = [
        {
            "type": "goal.started",
            "timestamp": "2026-07-30T00:00:00+00:00",
            "payload": {"task": "inspect page"},
        },
        {
            "type": "model.responded",
            "timestamp": "2026-07-30T00:00:01+00:00",
            "payload": {},
        },
        {
            "type": "observation.compiled",
            "timestamp": "not-a-timestamp",
            "payload": {},
        },
    ]

    metrics = extract_event_metrics(events)

    assert metrics.duration_ms == 1000
    assert metrics.terminal_status == "missing"
    assert metrics.model_turn_count == 1
    assert metrics.tool_call_count == 0
    assert metrics.policy_block_count == 0
    assert metrics.approval_request_count == 0
    assert metrics.observation_count == 1
    assert metrics.error_count == 0
    assert metrics.final_answer == ""


def _event(
    event_type: str,
    timestamp: datetime,
    payload: dict[str, Any] | None = None,
) -> EventRecord:
    return EventRecord(
        type=event_type,  # type: ignore[arg-type]
        source="test",
        timestamp=timestamp,
        payload=payload or {},
        session_id="session-1",
        task_id="task-1",
        goal_id="task-1",
    )
