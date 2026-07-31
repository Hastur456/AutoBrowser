"""Batch task loading and execution for AutoBrowser sessions."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class BatchTask:
    """One task entry from a batch tasks JSONL file."""

    scenario_id: str
    task: str
    expected: Any | None = None
    tags: list[str] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "task": self.task,
            "expected": self.expected,
            "tags": list(self.tags or []),
        }


class BatchSessionRuntime(Protocol):
    """SessionRuntime subset needed by batch execution."""

    context: Any

    async def run_task(self, task: str) -> Any:
        """Run one task inside the current session."""

    async def close(self) -> None:
        """Release session resources."""


def load_batch_tasks(path: Path) -> list[BatchTask]:
    """Load batch tasks from JSONL, one object per non-empty line."""

    tasks: list[BatchTask] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError(f"Line {line_number} must be a JSON object.")
        tasks.append(_batch_task_from_payload(payload, line_number=line_number))
    return tasks


async def run_batch(
    *,
    tasks_path: Path,
    session: BatchSessionRuntime,
    batches_dir: Path = Path(".autobrowser") / "batches",
    batch_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Run JSONL tasks sequentially through one SessionRuntime and write artifacts."""

    tasks_file = Path(tasks_path)
    tasks = load_batch_tasks(tasks_file)
    batch_id = batch_id or f"batch-{uuid4().hex}"
    batch_dir = Path(batches_dir) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tasks_file, batch_dir / "tasks.jsonl")

    started_at = _now_iso()
    metadata: dict[str, Any] = {
        "batch_id": batch_id,
        "started_at": started_at,
        "finished_at": None,
        "config": dict(config or {}),
        "tasks_path": str(tasks_file),
        "task_count": len(tasks),
    }
    _write_json(batch_dir / "batch.json", metadata)

    run_index_path = batch_dir / "run_index.jsonl"
    run_index_path.write_text("", encoding="utf-8")
    status = "completed"

    try:
        for task in tasks:
            row_started_at = _now_iso()
            try:
                await session.run_task(task.task)
            except Exception as exc:
                status = "failed"
                _append_jsonl(
                    run_index_path,
                    _run_index_row(
                        batch_id=batch_id,
                        task=task,
                        session=session,
                        status="failed",
                        started_at=row_started_at,
                        finished_at=_now_iso(),
                        error=_exception_payload(exc),
                    ),
                )
                if not continue_on_error:
                    raise
            else:
                _append_jsonl(
                    run_index_path,
                    _run_index_row(
                        batch_id=batch_id,
                        task=task,
                        session=session,
                        status="completed",
                        started_at=row_started_at,
                        finished_at=_now_iso(),
                        error=None,
                    ),
                )
    finally:
        metadata["finished_at"] = _now_iso()
        metadata["status"] = status
        _write_json(batch_dir / "batch.json", metadata)
        await session.close()

    return {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "batch_path": str(batch_dir / "batch.json"),
        "run_index_path": str(run_index_path),
        "status": status,
        "task_count": len(tasks),
    }


def _batch_task_from_payload(payload: Mapping[str, Any], *, line_number: int) -> BatchTask:
    scenario_id = payload.get("scenario_id")
    task = payload.get("task")
    if scenario_id in (None, ""):
        raise ValueError(f"Line {line_number} is missing scenario_id.")
    if task in (None, ""):
        raise ValueError(f"Line {line_number} is missing task.")
    tags = payload.get("tags")
    if tags is None:
        parsed_tags: list[str] | None = []
    elif isinstance(tags, list):
        parsed_tags = [str(item) for item in tags]
    else:
        raise ValueError(f"Line {line_number} tags must be a list.")
    return BatchTask(
        scenario_id=str(scenario_id),
        task=str(task),
        expected=payload.get("expected"),
        tags=parsed_tags,
    )


def _run_index_row(
    *,
    batch_id: str,
    task: BatchTask,
    session: BatchSessionRuntime,
    status: str,
    started_at: str,
    finished_at: str,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "scenario_id": task.scenario_id,
        "session_id": _session_id(session),
        "task_id": _latest_task_id(session),
        "status": status,
        "session_dir": _session_dir(session),
        "task": task.task,
        "expected": task.expected,
        "tags": list(task.tags or []),
        "started_at": started_at,
        "finished_at": finished_at,
        "error": error,
    }


def _session_id(session: BatchSessionRuntime) -> str | None:
    return _string_or_none(getattr(session.context, "session_id", None))


def _session_dir(session: BatchSessionRuntime) -> str | None:
    return _string_or_none(getattr(session.context, "session_dir", None))


def _latest_task_id(session: BatchSessionRuntime) -> str | None:
    tasks = getattr(session.context, "tasks", [])
    if not tasks:
        return None
    return _string_or_none(getattr(tasks[-1], "task_id", None))


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _exception_payload(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


__all__ = [
    "BatchSessionRuntime",
    "BatchTask",
    "load_batch_tasks",
    "run_batch",
]
