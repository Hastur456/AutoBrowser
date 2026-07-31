from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.agent_loop.events import (
    AgentTraceSink,
    CompositeEventSink,
    EventEmitter,
    EventRecord,
    InMemoryEventSink,
    JsonlEventSink,
    REDACTED_VALUE,
    redact_json_safe,
)


def test_event_emitter_assigns_context_and_sequence() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")

    first = emitter.emit(
        "session.started",
        source="test",
        payload={"ok": True},
    )
    second = emitter.emit(
        "goal.started",
        source="test",
        task_id="task-1",
        goal_id="task-1",
    )

    assert sink.records == [first, second]
    assert first.session_id == "session-1"
    assert second.sequence == 2
    assert second.task_id == "task-1"


def test_event_record_serializes_to_redacted_json() -> None:
    record = EventRecord(
        type="tool.finished",
        source="test",
        timestamp=datetime(2026, 7, 27, tzinfo=UTC),
        payload={
            "authorization": "Bearer secret",
            "nested": {"api_key": "abc", "visible": Path("trace.txt")},
        },
    )

    payload = record.to_json_dict()

    assert payload["timestamp"] == "2026-07-27T00:00:00+00:00"
    assert payload["payload"]["authorization"] == REDACTED_VALUE
    assert payload["payload"]["nested"]["api_key"] == REDACTED_VALUE
    assert payload["payload"]["nested"]["visible"] == "trace.txt"
    json.dumps(payload)


def test_jsonl_event_sink_writes_loadable_records(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    emitter = EventEmitter(sink, session_id="session-1")

    emitter.emit("session.started", source="test", payload={"token": "secret"})
    emitter.emit("session.closed", source="test")

    lines = path.read_text(encoding="utf-8").splitlines()
    loaded = [json.loads(line) for line in lines]

    assert [event["type"] for event in loaded] == ["session.started", "session.closed"]
    assert loaded[0]["payload"]["token"] == REDACTED_VALUE


def test_agent_trace_sink_projects_high_level_events_only(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    trace_path = tmp_path / "agent_trace.jsonl"
    sink = CompositeEventSink([JsonlEventSink(events_path), AgentTraceSink(trace_path)])
    emitter = EventEmitter(sink, session_id="session-1")

    emitter.emit(
        "goal.started",
        source="test",
        payload={"task": "inspect page"},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "graph.node_finished",
        source="test",
        payload={"node": "agent", "update": {"decision": "tool_call"}},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "action.proposed",
        source="test",
        payload={
            "tool_request": {
                "name": "browser.goto",
                "args": {"url": "https://ozon.ru", "authorization": "secret"},
                "reason": "Open the target page.",
            }
        },
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "tool.finished",
        source="test",
        payload={
            "tool_result": {
                "name": "browser.goto",
                "status": "success",
                "content": "x" * 2_000,
            }
        },
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "observation.compiled",
        source="test",
        payload={
            "observation": "Search page opened successfully.\n\nReady for the next step.",
            "has_snapshot": True,
            "snapshot": "ignored in projection",
        },
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "goal.completed",
        source="test",
        payload={"task": "inspect page", "result": {"final_answer": "done"}},
        task_id="task-1",
        goal_id="task-1",
    )

    events_lines = events_path.read_text(encoding="utf-8").splitlines()
    trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
    trace_events = [json.loads(line) for line in trace_lines]

    assert len(events_lines) == 6
    assert [event["type"] for event in trace_events] == [
        "goal.started",
        "action.proposed",
        "tool.finished",
        "observation.compiled",
        "goal.completed",
    ]
    assert trace_events[1] == {
        "timestamp": trace_events[1]["timestamp"],
        "type": "action.proposed",
        "tool": "browser.goto",
        "arguments": {"url": "https://ozon.ru", "authorization": REDACTED_VALUE},
        "reason": "Open the target page.",
    }
    assert trace_events[2] == {
        "timestamp": trace_events[2]["timestamp"],
        "type": "tool.finished",
        "tool": "browser.goto",
        "status": "success",
    }
    assert trace_events[3] == {
        "timestamp": trace_events[3]["timestamp"],
        "type": "observation.compiled",
        "summary": "Search page opened successfully. Ready for the next step.",
        "has_snapshot": True,
    }
    assert trace_events[4] == {
        "timestamp": trace_events[4]["timestamp"],
        "type": "goal.completed",
        "task": "inspect page",
        "final_answer": "done",
    }
    assert trace_path.stat().st_size < events_path.stat().st_size


def test_composite_event_sink_preserves_events_jsonl_bytes(tmp_path: Path) -> None:
    direct_events_path = tmp_path / "direct-events.jsonl"
    composite_events_path = tmp_path / "composite-events.jsonl"
    trace_path = tmp_path / "agent_trace.jsonl"

    direct_sink = JsonlEventSink(direct_events_path)
    composite_sink = CompositeEventSink(
        [
            JsonlEventSink(composite_events_path),
            AgentTraceSink(trace_path),
        ]
    )

    records = [
        EventRecord(
            type="goal.started",
            source="test",
            payload={"task": "inspect page"},
            event_id="event-1",
            timestamp=datetime(2026, 7, 27, tzinfo=UTC),
            session_id="session-1",
            task_id="task-1",
            goal_id="task-1",
            sequence=1,
        ),
        EventRecord(
            type="action.proposed",
            source="test",
            payload={
                "tool_request": {
                    "name": "browser_snapshot",
                    "args": {},
                    "reason": "Need refs.",
                }
            },
            event_id="event-2",
            timestamp=datetime(2026, 7, 27, 0, 0, 1, tzinfo=UTC),
            session_id="session-1",
            task_id="task-1",
            goal_id="task-1",
            sequence=2,
        ),
        EventRecord(
            type="tool.finished",
            source="test",
            payload={
                "tool_result": {
                    "name": "browser_snapshot",
                    "status": "success",
                    "content": "- link",
                }
            },
            event_id="event-3",
            timestamp=datetime(2026, 7, 27, 0, 0, 2, tzinfo=UTC),
            session_id="session-1",
            task_id="task-1",
            goal_id="task-1",
            sequence=3,
        ),
        EventRecord(
            type="goal.completed",
            source="test",
            payload={"task": "inspect page", "result": {"final_answer": "done"}},
            event_id="event-4",
            timestamp=datetime(2026, 7, 27, 0, 0, 3, tzinfo=UTC),
            session_id="session-1",
            task_id="task-1",
            goal_id="task-1",
            sequence=4,
        ),
    ]

    for record in records:
        direct_sink.emit(record)
        composite_sink.emit(record)

    assert direct_events_path.read_text(encoding="utf-8") == composite_events_path.read_text(
        encoding="utf-8"
    )


def test_redact_json_safe_truncates_long_strings() -> None:
    value = redact_json_safe({"content": "x" * 20_010})

    assert value["content"].endswith("... [truncated]")
