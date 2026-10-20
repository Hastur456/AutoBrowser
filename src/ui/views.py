"""UI-neutral read models shared by every frontend (TUI, plain, print mode).

Frontends render these frozen views and never reach into ``SessionRuntime`` or the engine.
``FeedItem`` is one projected event of the live feed (:func:`src.ui.feed.project_event`);
``SessionView``/``TaskView``/``ServerView`` are what the :class:`SessionController` reports
about the session. Nothing here decides by a tool name: names are data that is only shown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

#: What a projected event means for a feed, decided by the event ``type`` alone.
FeedKind = Literal[
    "goal_started",
    "plan",
    "turn",
    "model",
    "call",
    "gate",
    "approval_requested",
    "approval_resolved",
    "tool_started",
    "tool_finished",
    "observation",
    "outcome",
    "memory",
    "other",
]

#: Terminal status of a task as a frontend shows it.
OutcomeStatus = Literal["completed", "blocked", "cancelled", "failed"]

#: Task status in ``/tasks``: a terminal status or still running.
TaskStatus = Literal["running", "completed", "blocked", "cancelled", "failed"]


@dataclass(frozen=True)
class FeedItem:
    """One event of the live feed, already redacted and sanitized for display.

    Only the fields the event carries are filled; the rest keep their empty defaults.
    """

    kind: FeedKind
    #: The source ``EventRecord.type`` (``"tool.finished"``, ...).
    event_type: str
    sequence: int = 0
    task_id: str = ""
    timestamp: datetime | None = None
    #: Tool name exactly as the server exposes it — shown, never interpreted.
    tool: str = ""
    server: str = ""
    #: Tool arguments (redacted, sanitized) of ``call``/``approval_requested`` items.
    args: Mapping[str, object] = field(default_factory=dict)
    #: The decision of a ``gate`` item: ``deny``/``ask``/``blocked``/``allow``.
    decision: str = ""
    #: ``success``/``error``/... for tool results, the goal status for ``outcome``.
    status: str = ""
    #: Free text: tool output, a block reason, the final answer, an observation.
    text: str = ""
    #: The decisive rule or hook id of a gate, if any.
    rule: str = ""
    #: Where a gate came from: ``hook``/``permission``/``guard``, or the approval resolver.
    origin: str = ""
    #: ``- Page URL:`` read from a tool output by its text format, if present.
    url: str = ""
    #: Model call phase (``plan``/``agent``/``approval``) of a ``model`` item.
    phase: str = ""
    turn: int | None = None
    turns: int | None = None
    steps: tuple[str, ...] = ()
    #: Per-step status of a ``plan`` item (``pending``/``in_progress``/``completed``), when
    #: the plan carries one; aligned with ``steps``.
    step_statuses: tuple[str, ...] = ()
    current_step: int | None = None
    replan_count: int | None = None
    #: The task text of ``goal_started``/``outcome`` items.
    task: str = ""


@dataclass(frozen=True)
class ServerView:
    """One configured MCP server as ``/mcp`` and the banner show it."""

    name: str
    state: str
    generation: int = 0
    transport: str = ""
    tool_count: int = 0
    stateful: bool = False
    error: str = ""
    is_browser: bool = False


@dataclass(frozen=True)
class TaskView:
    """One task of the session as ``/tasks`` shows it."""

    index: int
    task_id: str
    task: str
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    turns: int | None = None


@dataclass(frozen=True)
class SessionView:
    """The session summary behind the banner and the status line."""

    session_id: str
    version: str = ""
    model: str = ""
    provider: str = ""
    workspace: str = ""
    browser: str = ""
    servers: tuple[ServerView, ...] = ()
    permission_mode: str = ""
    hooks_enabled: bool = False
    memory_enabled: bool = False
    busy: bool = False
    current_task_id: str = ""
    turn_cap: int | None = None
    #: The session continues a stored one (``--continue``/``--resume``).
    resumed: bool = False
    #: Tasks the session has run, including those of a resumed session.
    task_count: int = 0


__all__ = [
    "FeedItem",
    "FeedKind",
    "OutcomeStatus",
    "ServerView",
    "SessionView",
    "TaskStatus",
    "TaskView",
]
