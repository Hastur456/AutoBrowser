from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent_loop.batch import BatchScenario, load_batch_scenarios, run_batch


class FakeBatchSession:
    def __init__(
        self,
        session_id: str,
        *,
        fail_on: set[str] | None = None,
        blocked_on: set[str] | None = None,
    ) -> None:
        self.blocked_on = blocked_on or set()
        self.context = SimpleNamespace(
            session_id=session_id,
            session_dir=Path(".autobrowser") / "sessions" / session_id,
            tasks=[],
            state={},
        )
        self.fail_on = fail_on or set()
        self.ran_tasks: list[str] = []
        self.closed = False

    async def run_task(self, task: str) -> SimpleNamespace:
        if self.context.state.get("closed"):
            raise AssertionError("state leaked from a closed session")
        self.ran_tasks.append(task)
        self.context.state["last_task"] = task
        task_id = f"task-{len(self.context.tasks) + 1}"
        self.context.tasks.append(SimpleNamespace(task_id=task_id))
        if task in self.fail_on:
            raise RuntimeError(f"failed: {task}")
        if task in self.blocked_on:
            # A guard terminal quotes the last observation, page text included.
            return SimpleNamespace(status="blocked", final_answer=f"Blocked: limit. Page: {task}")
        return SimpleNamespace(status="done", final_answer=f"done: {task}")

    async def close(self) -> None:
        self.closed = True
        self.context.state["closed"] = True


class FakeSessionFactory:
    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        blocked_on: set[str] | None = None,
    ) -> None:
        self.fail_on = fail_on or set()
        self.blocked_on = blocked_on or set()
        self.sessions: list[FakeBatchSession] = []

    def __call__(self) -> FakeBatchSession:
        session = FakeBatchSession(
            f"session-{len(self.sessions) + 1}",
            fail_on=self.fail_on,
            blocked_on=self.blocked_on,
        )
        self.sessions.append(session)
        return session


def test_load_batch_scenarios_reads_jsonl(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {
                "scenario_id": "scenario-1",
                "tasks": ["inspect page", "second task"],
                "expected": {"contains": "done"},
                "tags": ["smoke", 123],
            },
            {
                "scenario_id": "scenario-2",
                "tasks": ["third task"],
            },
        ],
    )

    scenarios = load_batch_scenarios(tasks_path)

    assert scenarios == [
        BatchScenario(
            scenario_id="scenario-1",
            tasks=["inspect page", "second task"],
            expected={"contains": "done"},
            tags=["smoke", "123"],
        ),
        BatchScenario(
            scenario_id="scenario-2",
            tasks=["third task"],
            expected=None,
            tags=[],
        ),
    ]


def test_load_batch_scenarios_rejects_missing_required_fields(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(tasks_path, [{"scenario_id": "scenario-1", "task": "old format"}])

    with pytest.raises(ValueError, match="tasks list"):
        load_batch_scenarios(tasks_path)


@pytest.mark.asyncio
async def test_run_batch_writes_one_run_index_row_per_scenario(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    original_tasks = [
        {
            "scenario_id": "scenario-1",
            "tasks": ["inspect page", "second task"],
            "expected": "done",
            "tags": ["smoke"],
        },
        {
            "scenario_id": "scenario-2",
            "tasks": ["third task"],
            "expected": None,
            "tags": [],
        },
    ]
    _write_jsonl(tasks_path, original_tasks)
    factory = FakeSessionFactory()

    result = await run_batch(
        tasks_path=tasks_path,
        session_factory=factory,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
        config={"model": "test-model", "no_mcp": True},
    )

    batch_dir = tmp_path / "batches" / "batch-1"
    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    rows = _read_jsonl(batch_dir / "run_index.jsonl")
    assert result["batch_id"] == "batch-1"
    assert result["status"] == "completed"
    assert result["scenario_count"] == 2
    assert result["task_count"] == 3
    assert [session.ran_tasks for session in factory.sessions] == [
        ["inspect page", "second task"],
        ["third task"],
    ]
    assert all(session.closed for session in factory.sessions)
    assert batch["batch_id"] == "batch-1"
    assert batch["status"] == "completed"
    assert batch["config"] == {"model": "test-model", "no_mcp": True}
    assert batch["scenario_count"] == 2
    assert batch["task_count"] == 3
    assert (batch_dir / "tasks.jsonl").read_text(encoding="utf-8") == tasks_path.read_text(
        encoding="utf-8"
    )
    assert [row["scenario_id"] for row in rows] == ["scenario-1", "scenario-2"]
    assert [row["session_id"] for row in rows] == ["session-1", "session-2"]
    assert "task_id" not in rows[0]
    assert [row["status"] for row in rows] == ["completed", "completed"]
    assert rows[0]["tasks"] == ["inspect page", "second task"]
    assert rows[0]["expected"] == "done"
    assert rows[0]["tags"] == ["smoke"]
    assert rows[0]["error"] is None


@pytest.mark.asyncio
async def test_run_batch_uses_different_sessions_for_different_scenarios(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "tasks": ["first"]},
            {"scenario_id": "scenario-2", "tasks": ["second"]},
        ],
    )
    factory = FakeSessionFactory()

    await run_batch(
        tasks_path=tasks_path,
        session_factory=factory,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
    )

    rows = _read_jsonl(tmp_path / "batches" / "batch-1" / "run_index.jsonl")
    assert rows[0]["session_id"] != rows[1]["session_id"]
    assert [session.context.session_id for session in factory.sessions] == [
        rows[0]["session_id"],
        rows[1]["session_id"],
    ]


@pytest.mark.asyncio
async def test_run_batch_runs_multiple_tasks_inside_one_scenario_session(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [{"scenario_id": "scenario-1", "tasks": ["first", "second", "third"]}],
    )
    factory = FakeSessionFactory()

    await run_batch(
        tasks_path=tasks_path,
        session_factory=factory,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
    )

    assert len(factory.sessions) == 1
    assert factory.sessions[0].ran_tasks == ["first", "second", "third"]
    assert [task.task_id for task in factory.sessions[0].context.tasks] == [
        "task-1",
        "task-2",
        "task-3",
    ]


@pytest.mark.asyncio
async def test_run_batch_isolates_session_state_between_scenarios(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "tasks": ["first"]},
            {"scenario_id": "scenario-2", "tasks": ["second"]},
        ],
    )
    factory = FakeSessionFactory()

    await run_batch(
        tasks_path=tasks_path,
        session_factory=factory,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
    )

    assert factory.sessions[0].context.state["last_task"] == "first"
    assert factory.sessions[1].context.state["last_task"] == "second"
    assert factory.sessions[0].context.state is not factory.sessions[1].context.state


@pytest.mark.asyncio
async def test_run_batch_records_failure_and_continues_with_fresh_session(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "tasks": ["first", "fail"]},
            {"scenario_id": "scenario-2", "tasks": ["recover"]},
        ],
    )
    factory = FakeSessionFactory(fail_on={"fail"})

    result = await run_batch(
        tasks_path=tasks_path,
        session_factory=factory,
        batches_dir=tmp_path / "batches",
        batch_id="batch-1",
        continue_on_error=True,
    )

    rows = _read_jsonl(tmp_path / "batches" / "batch-1" / "run_index.jsonl")
    batch = json.loads(
        (tmp_path / "batches" / "batch-1" / "batch.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert [session.ran_tasks for session in factory.sessions] == [
        ["first", "fail"],
        ["recover"],
    ]
    assert all(session.closed for session in factory.sessions)
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert rows[0]["session_id"] == "session-1"
    assert rows[1]["session_id"] == "session-2"
    assert rows[0]["error"] == {"type": "RuntimeError", "message": "failed: fail"}
    assert batch["status"] == "failed"


@pytest.mark.asyncio
async def test_run_batch_records_failure_closes_and_reraises(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "scenario-1", "tasks": ["fail"]},
            {"scenario_id": "scenario-2", "tasks": ["skipped"]},
        ],
    )
    factory = FakeSessionFactory(fail_on={"fail"})

    with pytest.raises(RuntimeError, match="failed: fail"):
        await run_batch(
            tasks_path=tasks_path,
            session_factory=factory,
            batches_dir=tmp_path / "batches",
            batch_id="batch-1",
            continue_on_error=False,
        )

    rows = _read_jsonl(tmp_path / "batches" / "batch-1" / "run_index.jsonl")
    batch = json.loads(
        (tmp_path / "batches" / "batch-1" / "batch.json").read_text(encoding="utf-8")
    )
    assert len(factory.sessions) == 1
    assert factory.sessions[0].ran_tasks == ["fail"]
    assert factory.sessions[0].closed is True
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == {"type": "RuntimeError", "message": "failed: fail"}
    assert batch["status"] == "failed"


@pytest.mark.asyncio
async def test_run_batch_records_the_final_answer_of_the_last_task(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(tasks_path, [{"scenario_id": "s1", "tasks": ["first", "second"]}])

    result = await run_batch(
        tasks_path=tasks_path, session_factory=FakeSessionFactory(), batches_dir=tmp_path / "b"
    )

    (row,) = _read_jsonl(Path(result["run_index_path"]))
    assert row["final_answer"] == "done: second"
    assert row["task_statuses"] == ["done", "done"]
    assert "grade" not in row
    assert "grades" not in result


@pytest.mark.asyncio
async def test_run_batch_grades_each_scenario_when_asked(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "pass", "tasks": ["report"], "expected": {"contains": "report"}},
            {"scenario_id": "fail", "tasks": ["report"], "expected": {"contains": "absent"}},
            {"scenario_id": "open", "tasks": ["report"], "expected": {"fields": ["x"]}},
            {"scenario_id": "none", "tasks": ["report"]},
            {"scenario_id": "typo", "tasks": ["report"], "expected": "report"},
            {"scenario_id": "crash", "tasks": ["boom"], "expected": {"contains": "boom"}},
        ],
    )

    result = await run_batch(
        tasks_path=tasks_path,
        session_factory=FakeSessionFactory(fail_on={"boom"}),
        batches_dir=tmp_path / "b",
        continue_on_error=True,
        grade=True,
    )

    rows = {row["scenario_id"]: row for row in _read_jsonl(Path(result["run_index_path"]))}
    assert {name: row["grade"]["status"] for name, row in rows.items()} == {
        "pass": "passed",
        "fail": "failed",
        "open": "ungraded",
        "none": "ungraded",
        "typo": "failed",
        "crash": "failed",
    }
    assert rows["crash"]["grade"]["checks"][0]["key"] == "run"
    assert rows["crash"]["final_answer"] is None
    assert rows["typo"]["grade"]["checks"][0]["key"] == "expected"
    assert result["grades"] == {"passed": 1, "failed": 3, "ungraded": 2}


@pytest.mark.asyncio
async def test_a_task_that_did_not_end_done_fails_the_grade(tmp_path: Path) -> None:
    """A blocked answer quotes the page; its text must not pass the checks."""

    tasks_path = tmp_path / "tasks.jsonl"
    _write_jsonl(
        tasks_path,
        [
            {"scenario_id": "last", "tasks": ["open Python"], "expected": {"contains": "Python"}},
            {
                "scenario_id": "middle",
                "tasks": ["open Python", "report"],
                "expected": {"contains": "report"},
            },
        ],
    )

    result = await run_batch(
        tasks_path=tasks_path,
        session_factory=FakeSessionFactory(blocked_on={"open Python"}),
        batches_dir=tmp_path / "b",
        grade=True,
    )

    rows = {row["scenario_id"]: row for row in _read_jsonl(Path(result["run_index_path"]))}
    assert rows["last"]["task_statuses"] == ["blocked"]
    assert rows["last"]["grade"]["status"] == "failed"
    assert rows["last"]["grade"]["checks"] == [
        {"key": "run", "status": "failed", "message": "task 1 of 1 ended 'blocked'"}
    ]
    assert rows["middle"]["task_statuses"] == ["blocked", "done"]
    assert rows["middle"]["grade"]["status"] == "failed"
    assert result["grades"] == {"passed": 0, "failed": 2, "ungraded": 0}


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
