"""Stored sessions on the command line: ``--continue``, ``--resume [id]`` and the listing
shared with ``autobrowser sessions``."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from src.config import get_settings
from src.harness.session_store import (
    SessionNotFoundError,
    StoredSession,
    find_session,
    latest_resumable,
    list_sessions,
    load_session_file,
    load_state,
    session_in_use,
)


class ResumeError(RuntimeError):
    """``--continue``/``--resume`` cannot pick a session; the message says why."""


def session_rows(sessions: Sequence[StoredSession], *, numbered: bool = False) -> list[str]:
    """One aligned line per session: id, last activity, tasks, resumable, last task."""

    rows = []
    for index, session in enumerate(sessions, start=1):
        prefix = f"{index:>3}  " if numbered else ""
        when = _when(session.last_activity or session.started_at)
        mark = "resume" if session.resumable else "      "
        task = session.last_task.replace("\n", " ")
        rows.append(
            f"{prefix}{session.session_id[:12]}  {when:<16}  {session.task_count:>3} tasks  "
            f"{mark}  {task}".rstrip()
        )
    return rows


def resolve_resume(
    args: argparse.Namespace,
    *,
    interactive: bool,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    sessions_dir: Path | None = None,
) -> Path | None:
    """The stored session directory ``--continue``/``--resume`` asks for, else ``None``.

    ``--resume`` without an id lists the most recent resumable sessions and asks for a
    number; without a terminal that is an error (name the id instead).
    """

    directory = sessions_dir or get_settings().storage.sessions_dir
    chosen = _choose(args, directory, interactive=interactive, read=read, write=write)
    if chosen is not None:
        _check_usable(chosen)
    return chosen


def _choose(
    args: argparse.Namespace,
    directory: Path,
    *,
    interactive: bool,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> Path | None:
    if getattr(args, "continue_session", False):
        latest = latest_resumable(directory)
        if latest is None:
            raise ResumeError(f"No session to continue in {directory}.")
        return latest.path
    reference = getattr(args, "resume", None)
    if reference is None:
        return None
    if reference:
        try:
            session = find_session(directory, reference)
        except SessionNotFoundError as exc:
            raise ResumeError(str(exc)) from exc
        if not session.resumable:
            if not session.task_count:
                raise ResumeError(f"Session {session.session_id[:12]} ran no task; nothing to resume.")
            raise ResumeError(
                f"Session {session.session_id[:12]} has no saved dialogue (state.json); "
                "it was recorded before storage.persist_state or with it switched off."
            )
        return session.path
    candidates = [s for s in list_sessions(directory) if s.resumable]
    candidates = candidates[: get_settings().ui.resume_list_size]
    if not candidates:
        raise ResumeError(f"No session to resume in {directory}.")
    if not interactive:
        raise ResumeError("--resume without an id needs a terminal; pass the session id.")
    write("Sessions:")
    for row in session_rows(candidates, numbered=True):
        write(row)
    while True:
        try:
            answer = read("Resume which session (number, empty to cancel)? ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer:
            raise ResumeError("No session chosen.")
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1].path
        write(f"Enter a number from 1 to {len(candidates)}.")


def _check_usable(session_dir: Path) -> None:
    """A damaged state file or a session open elsewhere is a clean error, not a traceback."""

    if session_in_use(session_dir):
        raise ResumeError(
            f"Session {session_dir.name[:12]} is open in another AutoBrowser process "
            f"(lock {session_dir / '.lock'})."
        )
    try:
        if not load_session_file(session_dir):
            raise ValueError("session.json is missing or unreadable")
        load_state(session_dir)
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise ResumeError(f"Session {session_dir.name[:12]} cannot be resumed: {exc}") from exc


def _when(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment is not None else "-"


__all__ = ["ResumeError", "resolve_resume", "session_rows"]
