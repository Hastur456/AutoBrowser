from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.agent_loop.events import EventEmitter, EventRecord, JsonlEventSink
from src.agent_loop.replay import (
    iter_action_sequence,
    load_events,
    print_action_sequence,
    summarize_trace,
)


def test_load_events_and_print_action_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    emitter = EventEmitter(JsonlEventSink(path), session_id="session-1")
    emitter.emit(
        "goal.started",
        source="test",
        payload={"task": "inspect page"},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "action.proposed",
        source="test",
        payload={"tool_request": {"name": "browser_snapshot", "args": {}}},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "tool.finished",
        source="test",
        payload={"tool_result": {"name": "browser_snapshot", "status": "success"}},
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

    events = load_events(path)

    assert [event.type for event in events] == [
        "goal.started",
        "action.proposed",
        "tool.finished",
        "goal.completed",
    ]
    assert print_action_sequence(events) == (
        "goal.started: inspect page\n"
        "1. browser_snapshot {} -> success\n"
        "goal.completed: done"
    )


def test_replay_and_export_scripts_run_from_repo_root(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    emitter = EventEmitter(JsonlEventSink(path), session_id="session-1")
    emitter.emit(
        "goal.started",
        source="test",
        payload={"task": "inspect page"},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "action.proposed",
        source="test",
        payload={"tool_request": {"name": "browser_snapshot", "args": {}}},
        task_id="task-1",
        goal_id="task-1",
    )
    emitter.emit(
        "tool.finished",
        source="test",
        payload={"tool_result": {"name": "browser_snapshot", "status": "success"}},
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

    replay = subprocess.run(
        [sys.executable, "scripts/replay_trace.py", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    exported = subprocess.run(
        [sys.executable, "scripts/export_agent_trace.py", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "1. browser_snapshot {} -> success" in replay.stdout
    export_payload = json.loads(exported.stdout)
    assert export_payload["summary"]["terminal_status"] == "completed"
    assert export_payload["actions"][0]["name"] == "browser_snapshot"


def test_summarize_trace_counts_repeats_and_policy_blocks() -> None:
    raw_events = [
        {"type": "goal.started", "source": "test", "payload": {"task": "click"}},
        {
            "type": "action.proposed",
            "source": "test",
            "payload": {"tool_request": {"name": "browser_click", "args": {"ref": "e1"}}},
        },
        {
            "type": "action.proposed",
            "source": "test",
            "payload": {"tool_request": {"name": "browser_click", "args": {"ref": "e1"}}},
        },
        {
            "type": "policy.decided",
            "source": "test",
            "payload": {"decision": "blocked"},
        },
        {"type": "goal.failed", "source": "test", "payload": {"error": "blocked"}},
    ]
    events = [
        load_events_from_dict(event)
        for event in raw_events
    ]

    summary = summarize_trace(events)
    actions = list(iter_action_sequence(events))

    assert [action.name for action in actions] == ["browser_click", "browser_click"]
    assert summary.terminal_status == "failed"
    assert summary.repeated_action_count == 1
    assert summary.policy_block_count == 1
    assert summary.trace_complete is True


def load_events_from_dict(event: dict[str, object]):
    return EventRecord.from_json_dict(json.loads(json.dumps(event)))
