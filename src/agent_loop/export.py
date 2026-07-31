"""Read-only session export collection for AutoBrowser observability data."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.agent_loop.events import EventRecord
from src.agent_loop.metrics import extract_event_metrics


def collect_session_export_rows(
    sessions_dir: Path = Path(".autobrowser") / "sessions",
    feedback_path: Path | None = None,
    batches_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Collect export rows from every session directory under ``sessions_dir``."""

    root = Path(sessions_dir)
    if not root.exists():
        return []
    feedback = _load_feedback_index(
        Path(feedback_path) if feedback_path is not None else root.parent / "feedback.jsonl"
    )
    batch_index = _load_batch_index(
        Path(batches_dir) if batches_dir is not None else root.parent / "batches"
    )
    rows: list[dict[str, Any]] = []
    for session_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        rows.extend(
            collect_session_export_rows_from_dir(
                session_dir,
                feedback=feedback,
                batch_index=batch_index,
            )
        )
    return rows


def collect_session_export_rows_from_dir(
    session_dir: Path,
    *,
    feedback: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    feedback_path: Path | None = None,
    batch_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    batches_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Collect export rows for one ``.autobrowser/sessions/<session_id>`` directory."""

    session_path = Path(session_dir)
    session = _load_json_object(session_path / "session.json")
    tasks = _load_tasks(session_path, session)
    events = _load_event_records(session_path / "events.jsonl")
    events_by_task_id = _group_events_by_task_id(events)
    session_id = _session_id(session, session_path, events)
    feedback_index = (
        feedback
        if feedback is not None
        else _load_feedback_index(
            Path(feedback_path)
            if feedback_path is not None
            else session_path.parent.parent / "feedback.jsonl"
        )
    )
    batch_run_index = (
        batch_index
        if batch_index is not None
        else _load_batch_index(
            Path(batches_dir)
            if batches_dir is not None
            else session_path.parent.parent / "batches"
        )
    )

    task_by_id = {
        str(task.get("task_id")): task
        for task in tasks
        if task.get("task_id") not in (None, "")
    }
    task_ids = sorted(set(task_by_id) | set(events_by_task_id))
    return [
        _export_row(
            session_id=session_id,
            session=session,
            task=task_by_id.get(task_id, {}),
            task_id=task_id,
            events=events_by_task_id.get(task_id, []),
            feedback=feedback_index.get((session_id, task_id)),
            batch=batch_run_index.get((session_id, task_id)),
        )
        for task_id in task_ids
    ]


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_tasks(session_dir: Path, session: Mapping[str, Any]) -> list[dict[str, Any]]:
    tasks_path = session_dir / "tasks.json"
    if tasks_path.exists():
        payload = json.loads(tasks_path.read_text(encoding="utf-8"))
    else:
        payload = session.get("tasks")
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _load_event_records(path: Path) -> list[EventRecord]:
    if not path.exists():
        return []
    records: list[EventRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if isinstance(data, dict):
            records.append(EventRecord.from_json_dict(data))
    return records


def _load_feedback_index(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    feedback: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            continue
        session_id = data.get("session_id")
        task_id = data.get("task_id")
        if session_id in (None, "") or task_id in (None, ""):
            continue
        feedback[(str(session_id), str(task_id))] = dict(data)
    return feedback


def _load_batch_index(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    root = Path(path)
    if not root.exists():
        return {}
    batch_index: dict[tuple[str, str], dict[str, Any]] = {}
    for run_index_path in sorted(root.glob("*/run_index.jsonl")):
        for record in _load_jsonl_objects(run_index_path):
            session_id = record.get("session_id")
            task_id = record.get("task_id")
            if session_id in (None, "") or task_id in (None, ""):
                continue
            batch_index[(str(session_id), str(task_id))] = dict(record)
    return batch_index


def _load_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if isinstance(data, dict):
            records.append(data)
    return records


def _group_events_by_task_id(
    events: Iterable[EventRecord],
) -> dict[str, list[EventRecord]]:
    grouped: dict[str, list[EventRecord]] = defaultdict(list)
    for event in events:
        if event.task_id:
            grouped[event.task_id].append(event)
    return dict(grouped)


def _session_id(
    session: Mapping[str, Any],
    session_dir: Path,
    events: list[EventRecord],
) -> str:
    if session.get("session_id"):
        return str(session["session_id"])
    for event in events:
        if event.session_id:
            return event.session_id
    return session_dir.name


def _export_row(
    *,
    session_id: str,
    session: Mapping[str, Any],
    task: Mapping[str, Any],
    task_id: str,
    events: list[EventRecord],
    feedback: Mapping[str, Any] | None,
    batch: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metrics = extract_event_metrics(events)
    return {
        "session_id": session_id,
        "task_id": task_id,
        "batch_id": _batch_value(batch, "batch_id"),
        "scenario_id": _batch_value(batch, "scenario_id"),
        "task": _task_text(task, events),
        "expected": _batch_value(batch, "expected"),
        "tags": _batch_tags(batch),
        "started_at": task.get("started_at") or _first_event_timestamp(events),
        "finished_at": task.get("finished_at") or _last_event_timestamp(events),
        "metrics": metrics.to_dict(),
        "feedback": dict(feedback) if feedback is not None else None,
        "session_config": dict(_mapping(session.get("config"))),
        "session_metadata": dict(_mapping(session.get("metadata"))),
        "result": task.get("result"),
        "error": _task_error(task),
    }


def _task_text(task: Mapping[str, Any], events: list[EventRecord]) -> str:
    if task.get("task"):
        return str(task["task"])
    for event in events:
        if event.payload.get("task"):
            return str(event.payload["task"])
    return ""


def _batch_value(batch: Mapping[str, Any] | None, key: str) -> Any:
    if batch is None:
        return None
    return batch.get(key)


def _batch_tags(batch: Mapping[str, Any] | None) -> list[Any]:
    if batch is None:
        return []
    tags = batch.get("tags")
    return list(tags) if isinstance(tags, list) else []


def _first_event_timestamp(events: list[EventRecord]) -> str | None:
    if not events:
        return None
    return min(event.timestamp for event in events).isoformat()


def _last_event_timestamp(events: list[EventRecord]) -> str | None:
    if not events:
        return None
    return max(event.timestamp for event in events).isoformat()


def _task_error(task: Mapping[str, Any]) -> Any:
    result = task.get("result")
    if isinstance(result, Mapping) and result.get("type") and result.get("message"):
        return dict(result)
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "collect_session_export_rows",
    "collect_session_export_rows_from_dir",
]
