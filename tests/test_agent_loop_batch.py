from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent_loop.batch import BatchTask, load_batch_tasks, run_batch


class FakeBatchSession:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.context = SimpleNamespace(
            session_id="session-1",
            session_dir=Path(".autobrowser") / "sessions" / "session-1",
            tasks=[],
        )
        self.fail_on = fail_on or set()
        self.ran_tasks: list[str] = []
        self.closed = False

    async def run_task(self, task: str) -> dict[str, str]:
        self.ran_tasks.append(task)
        task_id = f"task-{len(self.context.tasks) + 1}"
        self.context.tasks.append(SimpleNamespace(task_id=task_id))
        if task in self.fail_on:
            raise RuntimeError(f"failed: {task}")
        return {"final_answer": f"done: {task}"}

    async def close(self) -> None:
        self.closed = True


def test_load_batch_tasks_reads_jsonl(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {
                "scenario_id": "scenario-1",
                "task": "inspect page",
                "expected": {"contains": "done"},
                "tags": ["smoke", 123],
            },
            {
                "scenario_id": "scenario-2",
                "task": "second task",
            },
        ],
    )

    tasks = load_batch_tasks(tasks_path)

    assert tasks == [
        BatchTask(
            scenario_id="scenario-1",
            task="inspect page",
            expected={"contains": "done"},
            tags=["smoke", "123"],
        ),
        BatchTask(
            scenario_id="scenario-2",
            task="second task",
            expected=None,
            tags=[],
        ),
    ]


def test_load_batch_tasks_rejects_missing_required_fields(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(tasks_path, [{"scenario_id": "scenario-1"}])

    with pytest.raises(ValueError, match="missing task"):
        load_batch_tasks(tasks_path)


@pytest.mark.asyncio
async def test_run_batch_writes_metadata_task_copy_and_run_index(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    original_tasks = [
        {
            "scenario_id": "scenario-1",
            "task": "inspect page",
            "expected": "done",
            "tags": ["smoke"],
        },
        {
            "scenario_id": "scenario-2",
            "task": "second task",
            "expected": None,
            "tags": [],
        },
    ]
    _write_jsonl(tasks_path, original_tasks)
    session = FakeBatchSession()

    result = await run_batch(
        tasks_path=tasks_path,
        session=session,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
        config={"model": "test-model", "no_mcp": True},
    )

    batch_dir = tmp_path / "batches" / "batch-1"
    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    rows = _read_jsonl(batch_dir / "run_index.jsonl")
    assert result["batch_id"] == "batch-1"
    assert result["status"] == "completed"
    assert session.ran_tasks == ["inspect page", "second task"]
    assert session.closed is True
    assert batch["batch_id"] == "batch-1"
    assert batch["started_at"]
    assert batch["finished_at"]
    assert batch["status"] == "completed"
    assert batch["config"] == {"model": "test-model", "no_mcp": True}
    assert batch["task_count"] == 2
    assert (batch_dir / "tasks.jsonl").read_text(encoding="utf-8") == tasks_path.read_text(
        encoding="utf-8"
    )
    assert [row["scenario_id"] for row in rows] == ["scenario-1", "scenario-2"]
    assert [row["task_id"] for row in rows] == ["task-1", "task-2"]
    assert {row["status"] for row in rows} == {"completed"}
    assert rows[0]["batch_id"] == "batch-1"
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["session_dir"].endswith(".autobrowser\\sessions\\session-1") or rows[0][
        "session_dir"
    ].endswith(".autobrowser/sessions/session-1")
    assert rows[0]["error"] is None


@pytest.mark.asyncio
async def test_run_batch_records_failure_and_continues(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "task": "fail"},
            {"scenario_id": "scenario-2", "task": "recover"},
        ],
    )
    session = FakeBatchSession(fail_on={"fail"})

    result = await run_batch(
        tasks_path=tasks_path,
        session=session,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
        continue_on_error=True,
    )

    rows = _read_jsonl(tmp_path / "batches" / "batch-1" / "run_index.jsonl")
    batch = json.loads(
        (tmp_path / "batches" / "batch-1" / "batch.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert session.ran_tasks == ["fail", "recover"]
    assert session.closed is True
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert rows[0]["error"] == {"type": "RuntimeError", "message": "failed: fail"}
    assert rows[0]["task_id"] == "task-1"
    assert rows[1]["task_id"] == "task-2"
    assert batch["status"] == "failed"
    assert batch["finished_at"]


@pytest.mark.asyncio
async def test_run_batch_records_failure_closes_and_reraises(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "task": "fail"},
            {"scenario_id": "scenario-2", "task": "skipped"},
        ],
    )
    session = FakeBatchSession(fail_on={"fail"})

    with pytest.raises(RuntimeError, match="failed: fail"):
        await run_batch(
            tasks_path=tasks_path,
            session=session,
            batches_dir=tmp_path / "batches",
            batch_id="batch-1",
            continue_on_error=False,
        )

    rows = _read_jsonl(tmp_path / "batches" / "batch-1" / "run_index.jsonl")
    batch = json.loads(
        (tmp_path / "batches" / "batch-1" / "batch.json").read_text(encoding="utf-8")
    )
    assert session.ran_tasks == ["fail"]
    assert session.closed is True
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == {"type": "RuntimeError", "message": "failed: fail"}
    assert batch["status"] == "failed"
    assert batch["finished_at"]


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
