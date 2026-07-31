from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.agent_loop.events import EventRecord
from src.agent_loop.export import (
    collect_session_export_rows,
    collect_session_export_rows_from_dir,
)


def test_collect_session_export_rows_reads_tasks_and_grouped_events(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "session-1"
    start = datetime(2026, 7, 30, tzinfo=UTC)
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "config": {"model": "test-model"},
            "metadata": {"task_count": 2},
        },
    )
    _write_json(
        session_dir / "tasks.json",
        [
            {
                "task_id": "task-1",
                "task": "inspect page",
                "started_at": "2026-07-30T00:00:00+00:00",
                "finished_at": "2026-07-30T00:00:05+00:00",
                "result": {"final_answer": "done"},
            },
            {
                "task_id": "task-2",
                "task": "second task",
                "started_at": "2026-07-30T00:01:00+00:00",
                "finished_at": None,
                "result": None,
            },
        ],
    )
    _write_events(
        session_dir / "events.jsonl",
        [
            _event("goal.started", start, task_id="task-1", payload={"task": "inspect page"}),
            _event("model.responded", start + timedelta(seconds=1), task_id="task-1"),
            _event(
                "tool.finished",
                start + timedelta(seconds=2),
                task_id="task-1",
                payload={"tool_result": {"status": "success"}},
            ),
            _event("observation.compiled", start + timedelta(seconds=3), task_id="task-1"),
            _event(
                "goal.completed",
                start + timedelta(seconds=5),
                task_id="task-1",
                payload={"result": {"final_answer": "done"}},
            ),
        ],
    )

    rows = collect_session_export_rows(sessions_dir)

    assert [row["task_id"] for row in rows] == ["task-1", "task-2"]
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["task"] == "inspect page"
    assert rows[0]["session_config"] == {"model": "test-model"}
    assert rows[0]["session_metadata"] == {"task_count": 2}
    assert rows[0]["metrics"] == {
        "duration_ms": 5000,
        "terminal_status": "completed",
        "model_turn_count": 1,
        "tool_call_count": 1,
        "policy_block_count": 0,
        "approval_request_count": 0,
        "observation_count": 1,
        "error_count": 0,
        "final_answer": "done",
    }
    assert rows[0]["feedback"] is None
    assert rows[1]["metrics"]["terminal_status"] == "missing"


def test_collect_session_export_rows_falls_back_to_session_tasks(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "session-1"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [
                {
                    "task_id": "task-1",
                    "task": "from session json",
                    "started_at": "2026-07-30T00:00:00+00:00",
                    "finished_at": None,
                    "result": None,
                }
            ],
        },
    )

    rows = collect_session_export_rows_from_dir(session_dir)

    assert len(rows) == 1
    assert rows[0]["task"] == "from session json"
    assert rows[0]["metrics"]["terminal_status"] == "missing"


def test_collect_session_export_rows_includes_event_only_task_ids(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "session-1"
    start = datetime(2026, 7, 30, tzinfo=UTC)
    _write_json(session_dir / "session.json", {"session_id": "session-1"})
    _write_events(
        session_dir / "events.jsonl",
        [
            _event("goal.started", start, task_id="task-9", payload={"task": "event task"}),
            _event(
                "goal.failed",
                start + timedelta(seconds=2),
                task_id="task-9",
                payload={"error": "failed"},
            ),
        ],
    )

    rows = collect_session_export_rows_from_dir(session_dir)

    assert len(rows) == 1
    assert rows[0]["task_id"] == "task-9"
    assert rows[0]["task"] == "event task"
    assert rows[0]["started_at"] == "2026-07-30T00:00:00+00:00"
    assert rows[0]["finished_at"] == "2026-07-30T00:00:02+00:00"
    assert rows[0]["metrics"]["terminal_status"] == "failed"
    assert rows[0]["metrics"]["error_count"] == 1


def test_collect_session_export_rows_joins_matched_feedback(tmp_path: Path) -> None:
    autobrowser_dir = tmp_path / ".autobrowser"
    sessions_dir = autobrowser_dir / "sessions"
    session_dir = sessions_dir / "session-1"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [{"task_id": "task-1", "task": "review me"}],
        },
    )
    _write_jsonl(
        autobrowser_dir / "feedback.jsonl",
        [
            {
                "session_id": "session-1",
                "task_id": "task-1",
                "scenario_id": "scenario-1",
                "rating": 5,
                "passed": True,
                "expected": "final answer",
                "notes": "ok",
                "reviewed_at": "2026-07-30T01:00:00+00:00",
            }
        ],
    )

    rows = collect_session_export_rows(sessions_dir)

    assert len(rows) == 1
    assert rows[0]["feedback"] == {
        "session_id": "session-1",
        "task_id": "task-1",
        "scenario_id": "scenario-1",
        "rating": 5,
        "passed": True,
        "expected": "final answer",
        "notes": "ok",
        "reviewed_at": "2026-07-30T01:00:00+00:00",
    }


def test_collect_session_export_rows_joins_batch_metadata(tmp_path: Path) -> None:
    autobrowser_dir = tmp_path / ".autobrowser"
    sessions_dir = autobrowser_dir / "sessions"
    session_dir = sessions_dir / "session-1"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [{"task_id": "task-1", "task": "inspect page"}],
        },
    )
    _write_jsonl(
        autobrowser_dir / "batches" / "batch-1" / "run_index.jsonl",
        [
            {
                "batch_id": "batch-1",
                "scenario_id": "scenario-1",
                "session_id": "session-1",
                "task_id": "task-1",
                "status": "completed",
                "expected": {"contains": "done"},
                "tags": ["smoke", "browser"],
            }
        ],
    )

    rows = collect_session_export_rows(sessions_dir)

    assert len(rows) == 1
    assert rows[0]["batch_id"] == "batch-1"
    assert rows[0]["scenario_id"] == "scenario-1"
    assert rows[0]["expected"] == {"contains": "done"}
    assert rows[0]["tags"] == ["smoke", "browser"]


def test_collect_session_export_rows_uses_batch_fallback_for_unbatched_task(
    tmp_path: Path,
) -> None:
    autobrowser_dir = tmp_path / ".autobrowser"
    session_dir = autobrowser_dir / "sessions" / "session-1"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [{"task_id": "task-1", "task": "inspect page"}],
        },
    )
    _write_jsonl(
        autobrowser_dir / "batches" / "batch-1" / "run_index.jsonl",
        [
            {
                "batch_id": "batch-1",
                "scenario_id": "scenario-other",
                "session_id": "session-1",
                "task_id": "other-task",
                "expected": "other",
                "tags": ["other"],
            }
        ],
    )

    rows = collect_session_export_rows_from_dir(session_dir)

    assert len(rows) == 1
    assert rows[0]["batch_id"] is None
    assert rows[0]["scenario_id"] is None
    assert rows[0]["expected"] is None
    assert rows[0]["tags"] == []


def test_collect_session_export_rows_leaves_unmatched_feedback_null(tmp_path: Path) -> None:
    autobrowser_dir = tmp_path / ".autobrowser"
    session_dir = autobrowser_dir / "sessions" / "session-1"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [{"task_id": "task-1", "task": "review me"}],
        },
    )
    _write_jsonl(
        autobrowser_dir / "feedback.jsonl",
        [
            {
                "session_id": "session-1",
                "task_id": "other-task",
                "rating": 1,
                "passed": False,
            }
        ],
    )

    rows = collect_session_export_rows_from_dir(session_dir)

    assert len(rows) == 1
    assert rows[0]["feedback"] is None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_events(path: Path, events: list[EventRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event.to_json_dict()) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _event(
    event_type: str,
    timestamp: datetime,
    *,
    task_id: str,
    payload: dict[str, Any] | None = None,
) -> EventRecord:
    return EventRecord(
        type=event_type,  # type: ignore[arg-type]
        source="test",
        timestamp=timestamp,
        payload=payload or {},
        session_id="session-1",
        task_id=task_id,
        goal_id=task_id,
    )
