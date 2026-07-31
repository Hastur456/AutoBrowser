"""Batch scenario loading and execution for AutoBrowser sessions."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class BatchScenario:
    """One scenario entry from a Golden Set JSONL file."""

    scenario_id: str
    tasks: list[str]
    expected: Any | None = None
    tags: list[str] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "tasks": list(self.tasks),
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


BatchSessionFactory = Callable[[], BatchSessionRuntime]


def load_batch_scenarios(path: Path) -> list[BatchScenario]:
    """Load Golden Set scenarios from JSONL, one object per non-empty line."""

    scenarios: list[BatchScenario] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError(f"Line {line_number} must be a JSON object.")
        scenarios.append(_batch_scenario_from_payload(payload, line_number=line_number))
    return scenarios


async def run_batch(
    *,
    tasks_path: Path,
    session_factory: BatchSessionFactory,
    batches_dir: Path = Path(".autobrowser") / "batches",
    batch_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Run Golden Set scenarios with one fresh SessionRuntime per scenario."""

    tasks_file = Path(tasks_path)
    scenarios = load_batch_scenarios(tasks_file)
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
        "scenario_count": len(scenarios),
        "task_count": sum(len(scenario.tasks) for scenario in scenarios),
    }
    _write_json(batch_dir / "batch.json", metadata)

    run_index_path = batch_dir / "run_index.jsonl"
    run_index_path.write_text("", encoding="utf-8")
    status = "completed"

    try:
        for scenario in scenarios:
            row_started_at = _now_iso()
            session = session_factory()
            try:
                for task in scenario.tasks:
                    await session.run_task(task)
            except Exception as exc:
                status = "failed"
                _append_jsonl(
                    run_index_path,
                    _run_index_row(
                        batch_id=batch_id,
                        scenario=scenario,
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
                        scenario=scenario,
                        session=session,
                        status="completed",
                        started_at=row_started_at,
                        finished_at=_now_iso(),
                        error=None,
                    ),
                )
            finally:
                await session.close()
    finally:
        metadata["finished_at"] = _now_iso()
        metadata["status"] = status
        _write_json(batch_dir / "batch.json", metadata)

    return {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "batch_path": str(batch_dir / "batch.json"),
        "run_index_path": str(run_index_path),
        "status": status,
        "scenario_count": len(scenarios),
        "task_count": sum(len(scenario.tasks) for scenario in scenarios),
    }


def _batch_scenario_from_payload(
    payload: Mapping[str, Any],
    *,
    line_number: int,
) -> BatchScenario:
    scenario_id = payload.get("scenario_id")
    tasks = payload.get("tasks")
    if scenario_id in (None, ""):
        raise ValueError(f"Line {line_number} is missing scenario_id.")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"Line {line_number} must define a non-empty tasks list.")
    if any(not isinstance(item, str) for item in tasks):
        raise ValueError(f"Line {line_number} tasks must contain strings.")
    parsed_tasks = [item.strip() for item in tasks]
    if any(not task for task in parsed_tasks):
        raise ValueError(f"Line {line_number} tasks must contain non-empty strings.")
    tags = payload.get("tags")
    if tags is None:
        parsed_tags: list[str] | None = []
    elif isinstance(tags, list):
        parsed_tags = [str(item) for item in tags]
    else:
        raise ValueError(f"Line {line_number} tags must be a list.")
    return BatchScenario(
        scenario_id=str(scenario_id),
        tasks=parsed_tasks,
        expected=payload.get("expected"),
        tags=parsed_tags,
    )


def _run_index_row(
    *,
    batch_id: str,
    scenario: BatchScenario,
    session: BatchSessionRuntime,
    status: str,
    started_at: str,
    finished_at: str,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "scenario_id": scenario.scenario_id,
        "session_id": _session_id(session),
        "status": status,
        "session_dir": _session_dir(session),
        "tasks": list(scenario.tasks),
        "expected": scenario.expected,
        "tags": list(scenario.tags or []),
        "started_at": started_at,
        "finished_at": finished_at,
        "error": error,
    }


def _session_id(session: BatchSessionRuntime) -> str | None:
    return _string_or_none(getattr(session.context, "session_id", None))


def _session_dir(session: BatchSessionRuntime) -> str | None:
    return _string_or_none(getattr(session.context, "session_dir", None))


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
    "BatchScenario",
    "BatchSessionFactory",
    "BatchSessionRuntime",
    "load_batch_scenarios",
    "run_batch",
]
