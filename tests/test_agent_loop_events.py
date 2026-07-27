from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.agent_loop.events import (
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


def test_redact_json_safe_truncates_long_strings() -> None:
    value = redact_json_safe({"content": "x" * 20_010})

    assert value["content"].endswith("... [truncated]")
