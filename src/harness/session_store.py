"""Durable session files: the carried state for ``--continue``/``--resume`` and the session list.

A session directory (``<storage.sessions_dir>/<session_id>/``) holds ``session.json`` and
``tasks.json`` (written by :meth:`SessionContext.persist`), ``events.jsonl`` and, with
``storage.persist_state``, ``state.json``: the :data:`SESSION_STATE_KEYS` a following task
inherits (dialogue messages, the latest observation, kept snapshots, last-action metadata).
That is what a resumed session continues from. It is stored as is, not redacted — the
dialogue must survive verbatim to be continued — so it is as private as the page content
the agent saw.

This module reads and writes files only; it builds no session objects (``session.py`` does).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.messages import Message, message_from_dict, message_to_dict

STATE_FILE_NAME = "state.json"
#: Holds the pid of the process that has the session open.
LOCK_FILE_NAME = ".lock"
#: What a resumed session carries over: the dialogue only. Page state (observation, kept
#: snapshots, last browser action) described a browser that is gone after a restart; kept,
#: it would point the model at dead refs and make permission rules resolve a stale domain.
RESUMED_STATE_KEYS = ("messages",)
SESSION_FILE_NAME = "session.json"
TASKS_FILE_NAME = "tasks.json"
EVENTS_FILE_NAME = "events.jsonl"
#: Bumped when the ``state.json`` layout changes incompatibly.
STATE_FORMAT_VERSION = 1


class SessionNotFoundError(LookupError):
    """No stored session matches the requested id (or the prefix is ambiguous)."""


class SessionInUseError(RuntimeError):
    """Another live process has the session open."""


@dataclass(frozen=True)
class StoredSession:
    """One session directory as ``autobrowser sessions`` and ``--resume`` list it."""

    session_id: str
    path: Path
    started_at: datetime | None
    last_activity: datetime | None
    task_count: int
    last_task: str
    #: The session ran a task and has ``state.json``, so it can be continued.
    resumable: bool


def save_state(session_dir: Path, session_id: str, state: Mapping[str, object]) -> Path:
    """Write ``state.json`` atomically; messages become plain JSON objects."""

    payload = {
        "version": STATE_FORMAT_VERSION,
        "session_id": session_id,
        "state": {key: _encode(key, value) for key, value in state.items()},
    }
    path = session_dir / STATE_FILE_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_state(session_dir: Path) -> dict[str, object]:
    """The carried state of ``state.json`` (``{}`` when there is none).

    Raises ``ValueError`` (``json.JSONDecodeError`` included) for a damaged file."""

    path = session_dir / STATE_FILE_NAME
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != STATE_FORMAT_VERSION:
        raise ValueError(f"{path} has an unsupported format (version {payload.get('version')!r}).")
    state = payload.get("state")
    if not isinstance(state, dict):
        return {}
    return {key: _decode(key, value) for key, value in state.items()}


def load_session_file(session_dir: Path) -> dict[str, Any]:
    return _read_json(session_dir / SESSION_FILE_NAME, default={})


def load_task_records(session_dir: Path) -> list[dict[str, Any]]:
    records = _read_json(session_dir / TASKS_FILE_NAME, default=[])
    return [record for record in records if isinstance(record, dict)]


def last_event_sequence(session_dir: Path) -> int:
    """The ``sequence`` of the last record in ``events.jsonl`` (0 without one), so a
    resumed session keeps numbering its events monotonically."""

    path = session_dir / EVENTS_FILE_NAME
    if not path.is_file():
        return 0
    # Only the last lines are parsed; a last line cut short by a crash falls back to the
    # one before it.
    tail: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if line.strip():
                tail = [*tail[-1:], line]
    for line in reversed(tail):
        try:
            sequence = json.loads(line).get("sequence")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(sequence, int):
            return sequence
    return 0


def acquire_lock(session_dir: Path) -> None:
    """Mark the session open by this process; refuse when another live process holds it."""

    holder = session_in_use(session_dir)
    if holder is not None and holder != os.getpid():
        raise SessionInUseError(
            f"Session {session_dir.name[:12]} is open in another process (pid {holder})."
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / LOCK_FILE_NAME).write_text(str(os.getpid()), encoding="utf-8")


def release_lock(session_dir: Path) -> None:
    path = session_dir / LOCK_FILE_NAME
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass


def session_in_use(session_dir: Path) -> int | None:
    """The pid of a live process holding the session, else ``None`` (a stale lock left by
    a crash does not count)."""

    try:
        pid = int((session_dir / LOCK_FILE_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        query_limited_information, still_active = 0x1000, 259
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_sessions(sessions_dir: Path) -> list[StoredSession]:
    """Stored sessions, the most recently active first."""

    if not sessions_dir.is_dir():
        return []
    sessions = [_summary(path) for path in sessions_dir.iterdir() if path.is_dir()]
    sessions = [session for session in sessions if session is not None]
    return sorted(sessions, key=_activity, reverse=True)


def find_session(sessions_dir: Path, reference: str) -> StoredSession:
    """The session whose id is ``reference`` or starts with it (an unambiguous prefix)."""

    sessions = list_sessions(sessions_dir)
    exact = [session for session in sessions if session.session_id == reference]
    if exact:
        return exact[0]
    matches = [session for session in sessions if session.session_id.startswith(reference)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionNotFoundError(f"No stored session matches {reference!r} in {sessions_dir}.")
    ids = ", ".join(session.session_id[:12] for session in matches[:5])
    raise SessionNotFoundError(f"{reference!r} matches several sessions: {ids}.")


def latest_resumable(sessions_dir: Path) -> StoredSession | None:
    return next((session for session in list_sessions(sessions_dir) if session.resumable), None)


def _summary(path: Path) -> StoredSession | None:
    data = load_session_file(path)
    if not data:
        return None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    tasks = load_task_records(path)
    return StoredSession(
        session_id=str(data.get("session_id") or path.name),
        path=path,
        started_at=_datetime(metadata.get("started_at")),
        last_activity=_datetime(metadata.get("last_activity")),
        task_count=len(tasks),
        last_task=str(tasks[-1].get("task") or "") if tasks else "",
        resumable=bool(tasks) and (path / STATE_FILE_NAME).is_file(),
    )


def _activity(session: StoredSession) -> datetime:
    moment = session.last_activity or session.started_at
    return moment if moment is not None else datetime.min.replace(tzinfo=UTC)


def _encode(key: str, value: object) -> object:
    if key == "messages" and isinstance(value, list):
        return [message_to_dict(message) for message in value if isinstance(message, Message)]
    return value


def _decode(key: str, value: object) -> object:
    if key == "messages" and isinstance(value, list):
        return [message_from_dict(item) for item in value if isinstance(item, dict)]
    return value


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _datetime(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = [
    "RESUMED_STATE_KEYS",
    "STATE_FILE_NAME",
    "SessionInUseError",
    "SessionNotFoundError",
    "acquire_lock",
    "StoredSession",
    "find_session",
    "last_event_sequence",
    "latest_resumable",
    "list_sessions",
    "load_session_file",
    "load_state",
    "load_task_records",
    "release_lock",
    "save_state",
    "session_in_use",
]
