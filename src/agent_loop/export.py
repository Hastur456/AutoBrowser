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
) -> list[dict[str, Any]]:
    """Collect export rows from every session directory under ``sessions_dir``."""

    root = Path(sessions_dir)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for session_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        rows.extend(collect_session_export_rows_from_dir(session_dir))
    return rows


def collect_session_export_rows_from_dir(session_dir: Path) -> list[dict[str, Any]]:
    """Collect export rows for one ``.autobrowser/sessions/<session_id>`` directory."""

    session_path = Path(session_dir)
    session = _load_json_object(session_path / "session.json")
    tasks = _load_tasks(session_path, session)
    events = _load_event_records(session_path / "events.jsonl")
    events_by_task_id = _group_events_by_task_id(events)
    session_id = _session_id(session, session_path, events)

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
) -> dict[str, Any]:
    metrics = extract_event_metrics(events)
    return {
        "session_id": session_id,
        "task_id": task_id,
        "task": _task_text(task, events),
        "started_at": task.get("started_at") or _first_event_timestamp(events),
        "finished_at": task.get("finished_at") or _last_event_timestamp(events),
        "metrics": metrics.to_dict(),
        "feedback": None,
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
