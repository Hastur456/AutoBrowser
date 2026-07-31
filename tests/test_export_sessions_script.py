from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.export_sessions import export_sessions, main
from src.agent_loop.events import EventRecord


def test_export_sessions_writes_jsonl_from_synthetic_session(tmp_path: Path) -> None:
    autobrowser_dir = tmp_path / ".autobrowser"
    sessions_dir = autobrowser_dir / "sessions"
    session_dir = sessions_dir / "session-1"
    out_path = tmp_path / "exports" / "runs.jsonl"
    start = datetime(2026, 7, 30, tzinfo=UTC)
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "config": {"model": "test-model"},
            "tasks": [{"task_id": "task-1", "task": "inspect page"}],
        },
    )
    _write_events(
        session_dir / "events.jsonl",
        [
            _event("goal.started", start, task_id="task-1", payload={"task": "inspect page"}),
            _event("model.responded", start + timedelta(seconds=1), task_id="task-1"),
            _event(
                "goal.completed",
                start + timedelta(seconds=3),
                task_id="task-1",
                payload={"result": {"final_answer": "done"}},
            ),
        ],
    )
    _write_jsonl(
        autobrowser_dir / "feedback.jsonl",
        [
            {
                "session_id": "session-1",
                "task_id": "task-1",
                "rating": 5,
                "passed": True,
            }
        ],
    )

    count = export_sessions(sessions_dir=sessions_dir, out_path=out_path)

    assert count == 1
    rows = _read_jsonl(out_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["task_id"] == "task-1"
    assert rows[0]["task"] == "inspect page"
    assert rows[0]["metrics"]["duration_ms"] == 3000
    assert rows[0]["metrics"]["terminal_status"] == "completed"
    assert rows[0]["metrics"]["final_answer"] == "done"
    assert rows[0]["feedback"] == {
        "session_id": "session-1",
        "task_id": "task-1",
        "rating": 5,
        "passed": True,
    }


def test_export_sessions_cli_accepts_explicit_feedback_path(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "session-1"
    feedback_path = tmp_path / "reviews" / "feedback.jsonl"
    out_path = tmp_path / "runs.jsonl"
    _write_json(
        session_dir / "session.json",
        {
            "session_id": "session-1",
            "tasks": [{"task_id": "task-1", "task": "inspect page"}],
        },
    )
    _write_jsonl(
        feedback_path,
        [
            {
                "session_id": "session-1",
                "task_id": "task-1",
                "rating": 4,
            }
        ],
    )

    exit_code = main(
        [
            "--sessions-dir",
            str(sessions_dir),
            "--feedback",
            str(feedback_path),
            "--out",
            str(out_path),
        ]
    )

    assert exit_code == 0
    rows = _read_jsonl(out_path)
    assert len(rows) == 1
    assert rows[0]["feedback"] == {
        "session_id": "session-1",
        "task_id": "task-1",
        "rating": 4,
    }


def test_export_sessions_writes_empty_jsonl_for_missing_sessions_dir(tmp_path: Path) -> None:
    out_path = tmp_path / "runs.jsonl"

    count = export_sessions(sessions_dir=tmp_path / "missing", out_path=out_path)

    assert count == 0
    assert out_path.read_text(encoding="utf-8") == ""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
