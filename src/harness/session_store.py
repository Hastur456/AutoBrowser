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

import errno
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from src.messages import Message, message_from_dict, message_to_dict

STATE_FILE_NAME = "state.json"
#: Locked (by the operating system) while a process has the session open.
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
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not hold a JSON object.")
    if payload.get("version") != STATE_FORMAT_VERSION:
        raise ValueError(f"{path} has an unsupported format (version {payload.get('version')!r}).")
    state = payload.get("state")
    if not isinstance(state, dict):
        return {}
    try:
        return {key: _decode(key, value) for key, value in state.items()}
    except (TypeError, AttributeError) as exc:
        raise ValueError(f"{path} holds a malformed message: {exc}") from exc


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


class SessionLock:
    """An OS-level exclusive lock on ``<session>/.lock``, held through an open descriptor.

    The operating system drops it when the holder exits, crashes or is killed, so a stale
    lock never blocks a later resume and a recycled pid cannot keep one alive; taking it is
    atomic, so two processes racing for one session cannot both win. The file itself is
    never deleted (unlinking a lock file another process has just opened would let two
    holders coexist).
    """

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle: BinaryIO | None = handle

    @classmethod
    def acquire(cls, session_dir: Path) -> SessionLock:
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / LOCK_FILE_NAME
        handle = path.open("a+b")
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            if exc.errno not in _LOCK_CONFLICT_ERRNOS:
                raise  # the file system cannot lock (e.g. NFS without lockd): say so
            raise SessionInUseError(
                f"Session {session_dir.name[:12]} is open in another AutoBrowser process "
                f"(lock {path})."
            ) from exc
        return cls(path, handle)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        handle.close()


def session_in_use(session_dir: Path) -> bool:
    """Whether a session is open (in another process, or by another holder in this one)."""

    if not (session_dir / LOCK_FILE_NAME).exists():
        return False
    try:
        SessionLock.acquire(session_dir).release()
    except SessionInUseError:
        return True
    return False


#: ``errno`` values that mean "somebody else holds the lock" (Windows reports EACCES or
#: EDEADLK from ``msvcrt.locking``, POSIX EWOULDBLOCK/EAGAIN from ``flock``).
_LOCK_CONFLICT_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK})


def _lock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    if key != "messages":
        return value
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("messages must be a list of message objects")
    return [message_from_dict(item) for item in value]


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
    "SessionLock",
    "SessionNotFoundError",
    "StoredSession",
    "find_session",
    "last_event_sequence",
    "latest_resumable",
    "list_sessions",
    "load_session_file",
    "load_state",
    "load_task_records",
    "save_state",
    "session_in_use",
]
