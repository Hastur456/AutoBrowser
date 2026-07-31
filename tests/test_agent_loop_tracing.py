from __future__ import annotations

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.tracing import emit_chunk_events, event_context_from_config


def test_event_context_from_config_prefers_private_metadata() -> None:
    context = event_context_from_config(
        {
            "metadata": {"session_id": "public", "task_id": "task-public"},
            "_autobrowser_event_metadata": {
                "session_id": "session-1",
                "task_id": "task-1",
                "goal_id": "goal-1",
            },
        }
    )

    assert context == {
        "session_id": "session-1",
        "task_id": "task-1",
        "goal_id": "goal-1",
    }


def test_emit_chunk_events_derives_action_policy_tool_and_observation_events() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    context = {"session_id": "session-1", "task_id": "task-1", "goal_id": "task-1"}

    emit_chunk_events(
        emitter,
        {
            "agent": {
                "decision": "tool_call",
                "tool_request": {"name": "browser_snapshot", "args": {}},
            },
            "policy": {
                "policy_event": {
                    "decision": "approved",
                    "reason": "ok",
                    "tool_request": {"name": "browser_snapshot", "args": {}},
                }
            },
            "executor": {
                "tool_result": {
                    "name": "browser_snapshot",
                    "status": "success",
                    "content": "- textbox Search ref=e1",
                    "error": "",
                }
            },
            "observe": {
                "observation": "Snapshot captured.",
                "snapshot": "- textbox Search ref=e1",
            },
        },
        context=context,
    )

    event_types = [record.type for record in sink.records]
    assert event_types.count("graph.node_started") == 4
    assert event_types.count("graph.node_finished") == 4
    assert event_types.count("model.responded") == 1
    assert "action.proposed" in event_types
    assert "policy.decided" in event_types
    assert "tool.started" in event_types
    assert "tool.finished" in event_types
    assert "observation.compiled" in event_types
    assert all(record.task_id == "task-1" for record in sink.records)
