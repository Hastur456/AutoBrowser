"""Batch scenario loading and execution for AutoBrowser sessions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from src.agent_loop.batch_report import (
    REPORT_FILE_NAME,
    render_batch_report,
    render_trace_text,
    total_metrics,
)
from src.agent_loop.benchmarks import summarize_run
from src.agent_loop.events import EventRecord, redact_json_safe
from src.agent_loop.failures import (
    TaskOutcome,
    classify_scenario,
    classify_tasks,
    outcome_from_result,
)
from src.agent_loop.graders import grade_answer
from src.agent_loop.replay import load_events
from src.config import Settings, get_settings
from src.harness.session_store import EVENTS_FILE_NAME

#: Version of the ``batch.json`` and ``run_index.jsonl`` row formats.
SCHEMA_VERSION = 1
#: Subdirectory of a batch directory holding a copy and a readable version of every trace.
TRACES_DIR_NAME = "traces"
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


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


class BatchTaskResult(Protocol):
    """The part of a task result batch execution reads (``AgentLoopResult`` has it)."""

    @property
    def status(self) -> str:
        """Terminal status of the task: ``done``, ``blocked`` or ``cancelled``."""

    @property
    def final_answer(self) -> str | None:
        """The task's final answer."""

    # ``stop_reason`` (``AgentLoopResult.stop_reason``) is read when present, not required.


class BatchSessionRuntime(Protocol):
    """SessionRuntime subset needed by batch execution."""

    context: Any

    async def run_task(self, task: str) -> BatchTaskResult:
        """Run one task inside the current session."""

    async def close(self) -> None:
        """Release session resources."""


#: Builds the fresh session of one scenario (the scenario names it, e.g. in session labels).
BatchSessionFactory = Callable[[BatchScenario], BatchSessionRuntime]


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
    batches_dir: Path | None = None,
    batch_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    continue_on_error: bool = False,
    grade: bool = False,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Golden Set scenarios with one fresh SessionRuntime per scenario.

    Each ``run_index.jsonl`` row records the terminal status of every task and the final
    answer of the scenario's last task. With ``grade``, it also holds the grade of that answer
    against the scenario's ``expected`` mapping (:func:`~src.agent_loop.graders.grade_answer`):
    a scenario that raised, or a task that did not end ``done`` (a ``blocked`` answer quotes
    the page it stopped on, so its text must not be graded), grades as failed; a scenario
    without ``expected`` as ungraded. The result then counts the grades.

    Every row also carries the ``failure_kind`` of the scenario and the ``task_outcomes`` it
    was derived from (:mod:`src.agent_loop.failures`), read from the session trace, or from
    the task results when the session left no trace. ``provenance`` (commit, model, settings
    fingerprint; built by the caller) is recorded in ``batch.json``.
    """

    tasks_file = Path(tasks_path)
    scenarios = load_batch_scenarios(tasks_file)
    batch_id = batch_id or f"batch-{uuid4().hex}"
    if batches_dir is None:
        batches_dir = get_settings().storage.batches_dir
    batch_dir = Path(batches_dir) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tasks_file, batch_dir / "tasks.jsonl")

    started_at = _now_iso()
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "started_at": started_at,
        "finished_at": None,
        "config": dict(config or {}),
        "tasks_path": str(tasks_file),
        "scenario_count": len(scenarios),
        "task_count": sum(len(scenario.tasks) for scenario in scenarios),
        "provenance": dict(provenance or {}),
    }
    _write_json(batch_dir / "batch.json", metadata)

    run_index_path = batch_dir / "run_index.jsonl"
    run_index_path.write_text("", encoding="utf-8")
    status = "completed"
    grade_counts = {"passed": 0, "failed": 0, "ungraded": 0}
    rows: list[dict[str, Any]] = []
    trace_names: set[str] = set()

    try:
        for scenario in scenarios:
            row_started_at = _now_iso()
            session = session_factory(scenario)
            final_answer: str | None = None
            task_statuses: list[str] = []
            stop_reasons: list[str] = []
            row: dict[str, Any] | None = None
            try:
                for task in scenario.tasks:
                    result = await session.run_task(task)
                    task_statuses.append(str(result.status))
                    stop_reasons.append(str(getattr(result, "stop_reason", "") or ""))
                    final_answer = result.final_answer
            except Exception as exc:
                status = "failed"
                row = _run_index_row(
                    batch_id=batch_id,
                    scenario=scenario,
                    session=session,
                    status="failed",
                    started_at=row_started_at,
                    finished_at=_now_iso(),
                    error=_exception_payload(exc),
                    final_answer=None,
                    task_statuses=task_statuses,
                )
                if grade:
                    row["grade"] = _failed_run_grade(
                        f"the scenario failed: {type(exc).__name__}: {exc}"
                    )
                    grade_counts["failed"] += 1
                if not continue_on_error:
                    raise
            else:
                row = _run_index_row(
                    batch_id=batch_id,
                    scenario=scenario,
                    session=session,
                    status="completed",
                    started_at=row_started_at,
                    finished_at=_now_iso(),
                    error=None,
                    final_answer=final_answer,
                    task_statuses=task_statuses,
                )
                if grade:
                    row["grade"] = _grade(scenario, final_answer, task_statuses)
                    grade_counts[row["grade"]["status"]] += 1
            finally:
                await session.close()
                if row is not None:
                    # After close: the session's trace is complete on disk.
                    trace = _session_trace(session)
                    events = load_events(trace) if trace is not None else None
                    row["metrics"] = _run_metrics(events, row)
                    outcomes = (
                        classify_tasks(events)
                        if events is not None
                        else _result_outcomes(session, task_statuses, stop_reasons)
                    )
                    _add_outcomes(row, outcomes)
                    row.update(
                        _keep_trace(batch_dir, scenario.scenario_id, trace, events, outcomes, trace_names)
                    )
                    rows.append(row)
                    _append_jsonl(run_index_path, row)
    finally:
        metadata["finished_at"] = _now_iso()
        metadata["status"] = status
        _write_json(batch_dir / "batch.json", metadata)
        (batch_dir / REPORT_FILE_NAME).write_text(
            render_batch_report(metadata, rows, graded=grade), encoding="utf-8"
        )

    return {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "batch_path": str(batch_dir / "batch.json"),
        "run_index_path": str(run_index_path),
        "status": status,
        "scenario_count": len(scenarios),
        "task_count": sum(len(scenario.tasks) for scenario in scenarios),
        "report_path": str(batch_dir / REPORT_FILE_NAME),
        "metrics": total_metrics(rows),
        **({"grades": grade_counts} if grade else {}),
    }


def settings_provenance(settings: Settings) -> dict[str, Any]:
    """Fingerprint of the effective settings and the sections that differ from the code.

    ``settings_sha256`` hashes the redacted settings, so no secret reaches it;
    ``non_default_sections`` names the sections a profile or the environment changed.
    """

    dumped = redact_json_safe(settings.model_dump(mode="json"))
    canonical = json.dumps(dumped, sort_keys=True, ensure_ascii=False)
    sections = sorted(
        name
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), BaseModel) and value != type(value)()
    )
    return {
        "settings_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "non_default_sections": sections,
    }


def _session_trace(session: BatchSessionRuntime) -> Path | None:
    """The session's trace file, or ``None`` when it left none (it raised before writing one)."""

    session_dir = _session_dir(session)
    trace = Path(session_dir) / EVENTS_FILE_NAME if session_dir else None
    return trace if trace is not None and trace.is_file() else None


def _keep_trace(
    batch_dir: Path,
    scenario_id: str,
    trace: Path | None,
    events: list[EventRecord] | None,
    outcomes: Sequence[TaskOutcome],
    used_names: set[str],
) -> dict[str, str | None]:
    """Copy the trace into ``traces/`` beside a readable version; paths are batch-relative."""

    if trace is None or events is None:
        return {"trace_path": None, "trace_text_path": None}
    name = _trace_name(scenario_id, used_names)
    traces_dir = batch_dir / TRACES_DIR_NAME
    traces_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(trace, traces_dir / f"{name}.events.jsonl")
    (traces_dir / f"{name}.txt").write_text(
        render_trace_text(scenario_id, events, outcomes), encoding="utf-8"
    )
    return {
        "trace_path": f"{TRACES_DIR_NAME}/{name}.events.jsonl",
        "trace_text_path": f"{TRACES_DIR_NAME}/{name}.txt",
    }


def _trace_name(scenario_id: str, used_names: set[str]) -> str:
    """A file-safe name for the scenario, unique within the batch (case-insensitively)."""

    base = _UNSAFE_NAME_CHARS.sub("_", scenario_id) or "_"
    name, suffix = base, 1
    while name.casefold() in used_names:
        suffix += 1
        name = f"{base}-{suffix}"
    used_names.add(name.casefold())
    return name


def _run_metrics(events: list[EventRecord] | None, row: Mapping[str, Any]) -> dict[str, Any]:
    """Wall time of the scenario plus the figures of its session trace (when there is one)."""

    started = datetime.fromisoformat(str(row["started_at"]))
    finished = datetime.fromisoformat(str(row["finished_at"]))
    metrics: dict[str, Any] = {"duration_ms": int((finished - started).total_seconds() * 1000)}
    if events is not None:
        metrics.update(summarize_run(events).to_json_dict())
    return metrics


def _result_outcomes(
    session: BatchSessionRuntime,
    task_statuses: Sequence[str],
    stop_reasons: Sequence[str],
) -> list[TaskOutcome]:
    """Outcomes of the tasks that returned a result, for a session without a trace."""

    records = list(getattr(session.context, "tasks", None) or [])
    return [
        outcome_from_result(
            str(getattr(records[index], "task_id", "") or "") if index < len(records) else "",
            status,
            reason,
        )
        for index, (status, reason) in enumerate(zip(task_statuses, stop_reasons, strict=True))
    ]


def _add_outcomes(row: dict[str, Any], outcomes: Sequence[TaskOutcome]) -> None:
    error = row.get("error")
    row["schema_version"] = SCHEMA_VERSION
    row["task_outcomes"] = [asdict(outcome) for outcome in outcomes]
    row["failure_kind"] = classify_scenario(
        outcomes,
        (row.get("grade") or {}).get("status"),
        error_type=str(error.get("type") or "") if error else None,
    )


def _grade(
    scenario: BatchScenario,
    final_answer: str | None,
    task_statuses: list[str],
) -> dict[str, Any]:
    unfinished = [
        f"task {index} of {len(task_statuses)} ended {status!r}"
        for index, status in enumerate(task_statuses, start=1)
        if status != "done"
    ]
    if unfinished:
        return _failed_run_grade("; ".join(unfinished))
    if scenario.expected is None:
        return {"status": "ungraded", "checks": []}
    if not isinstance(scenario.expected, Mapping):
        # A typo such as "expected": "Python" must not silently stop the scenario's checks.
        return {
            "status": "failed",
            "checks": [
                {
                    "key": "expected",
                    "status": "failed",
                    "message": f"expected must be a mapping of checks, not {scenario.expected!r}",
                }
            ],
        }
    return grade_answer(final_answer or "", scenario.expected).to_json_dict()


def _failed_run_grade(message: str) -> dict[str, Any]:
    return {"status": "failed", "checks": [{"key": "run", "status": "failed", "message": message}]}


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
    final_answer: str | None,
    task_statuses: list[str],
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
        "task_statuses": list(task_statuses),
        "final_answer": final_answer,
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
    "SCHEMA_VERSION",
    "TRACES_DIR_NAME",
    "BatchScenario",
    "BatchSessionFactory",
    "BatchSessionRuntime",
    "BatchTaskResult",
    "load_batch_scenarios",
    "run_batch",
    "settings_provenance",
]
