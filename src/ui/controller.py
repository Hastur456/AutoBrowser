"""The UI-neutral session facade every front end talks to.

:class:`SessionController` wraps one :class:`~src.harness.session.SessionRuntime` and offers
the commands (submit, cancel, reset, reconnect, answer an approval) and the read models
(:mod:`src.ui.views`) a terminal UI, the plain front end, print mode or the cmd2 shell need,
through the runtime's public API only. Its coroutines run on the session loop
(:class:`~src.ui.runtime_loop.RuntimeLoop`); the synchronous readers and :meth:`answer` are
safe from any thread. It decides nothing about tasks: the engine and the ``GoalRunner`` do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from importlib import metadata
from typing import TYPE_CHECKING, Any

from src.contracts import (
    ApprovalAnswer,
    ApprovalTicket,
    ApprovalVia,
    MemoryEntry,
)
from src.harness.approvals import ApprovalBroker, ApprovalListener
from src.harness.event_stream import EventBroadcaster, StreamCursor, StreamItem
from src.harness.hooks import HookEngine
from src.harness.session import SessionContext, SessionRuntime, TaskRecord
from src.mcp import ConnectionState
from src.messages import Message
from src.ui.views import ServerView, SessionView, TaskStatus, TaskView

if TYPE_CHECKING:
    from src.harness.session import AgentLoopResult

logger = logging.getLogger(__name__)

#: Distribution name whose installed version the banner shows.
PACKAGE_NAME = "autobrowser"

_TASK_STATUSES: frozenset[str] = frozenset(
    {"running", "completed", "blocked", "cancelled", "failed"}
)


class TaskBusyError(RuntimeError):
    """A task is already running; one session runs one task at a time."""


class SessionController:
    """Commands and read models over one session runtime, for any front end."""

    def __init__(
        self,
        runtime: SessionRuntime,
        *,
        broker: ApprovalBroker | None = None,
        broadcaster: EventBroadcaster | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._broker = broker
        self._broadcaster = broadcaster
        if output_fn is not None:
            runtime.set_output_fn(output_fn)
        self._task: asyncio.Task[AgentLoopResult] | None = None
        self._attached: SessionContext | None = None

    # ------------------------------------------------------------------ commands

    async def start(self) -> SessionView:
        """Start the session (Chrome, MCP, the agent) if it is not running yet."""

        self._attach()
        await self._runtime.start()
        return self.session_view()

    async def submit(self, text: str) -> TaskView:
        """Start ``text`` as the session's next task and return it once it is tracked.

        Raises :class:`TaskBusyError` while another task runs. The task runs on in the
        background; its progress and outcome arrive on :meth:`subscribe`.
        """

        if self.busy:
            raise TaskBusyError("A task is already running.")
        await self.start()
        known = len(self._runtime.context.tasks)
        self._task = asyncio.create_task(self._runtime.run_task(text))
        self._task.add_done_callback(_log_task_failure)
        # ``run_task`` tracks the record before its first real suspension.
        while len(self._runtime.context.tasks) == known and not self._task.done():
            await asyncio.sleep(0)
        tasks = self._runtime.context.tasks
        if len(tasks) == known:
            return TaskView(index=0, task_id="", task=text, status="failed")
        return _task_view(len(tasks), tasks[-1])

    async def run(self, text: str) -> AgentLoopResult:
        """Submit ``text`` and wait for its terminal result (raises what the task raised)."""

        await self.submit(text)
        task = self._task
        assert task is not None
        return await task

    async def wait(self) -> TaskView | None:
        """Wait for the running task, if any, and return the last task's view."""

        task = self._task
        if task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
        views = self.task_views()
        return views[-1] if views else None

    async def cancel(self) -> bool:
        """Cancel the running task; ``False`` when none runs."""

        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return True

    async def reconnect(self, server: str) -> ServerView:
        """Restart one MCP server and return its new state."""

        if self.busy:
            raise TaskBusyError("Cannot reconnect while a task is running.")
        mcp = self._runtime.context.mcp
        if mcp is None:
            raise RuntimeError("MCP is not started.")
        await mcp.manager.reconnect(server)
        for view in self._server_views():
            if view.name == server:
                return view
        raise KeyError(server)

    async def reset(self) -> SessionView:
        """Close the session and continue in a fresh one (new id, no history, no tasks)."""

        if self.busy:
            raise TaskBusyError("Cannot reset while a task is running.")
        context = await self._runtime.reset()
        if self._broadcaster is not None:
            self._broadcaster.mark_reset(context.session_id)
        self._attach()
        return self.session_view()

    def answer(self, ticket_id: str, answer: ApprovalAnswer, *, via: ApprovalVia) -> bool:
        """Answer a pending approval; thread-safe. ``False`` when it is not answerable."""

        if self._broker is None:
            return False
        return self._broker.answer(ticket_id, answer, via=via)

    # ------------------------------------------------------------------ streams

    def subscribe(self, after: StreamCursor | None = None) -> AsyncIterator[StreamItem]:
        """The session's event stream (see :class:`EventBroadcaster.subscribe`)."""

        if self._broadcaster is None:
            raise RuntimeError("This controller was built without an event stream.")
        return self._broadcaster.subscribe(after)

    def subscribe_approvals(self, listener: ApprovalListener) -> Callable[[], None]:
        """New tickets and their resolutions; ``listener`` runs on the session loop thread."""

        if self._broker is None:
            return lambda: None
        return self._broker.subscribe(listener)

    def pending_approvals(self) -> tuple[ApprovalTicket, ...]:
        """Approvals waiting for an answer, oldest first."""

        return self._broker.pending() if self._broker is not None else ()

    # ------------------------------------------------------------------ read models

    @property
    def busy(self) -> bool:
        """A task is running."""

        return self._task is not None and not self._task.done()

    def session_view(self) -> SessionView:
        context = self._runtime.context
        config = self._runtime.config
        workspace = context.workspace.root if context.workspace is not None else None
        current = context.tasks[-1] if self.busy and context.tasks else None
        return SessionView(
            session_id=context.session_id,
            version=_package_version(),
            model=config.model,
            provider="",
            workspace=str(workspace) if workspace is not None else os.getcwd(),
            browser=self._browser_state(),
            servers=tuple(self._server_views()),
            permission_mode=context.permissions.mode,
            hooks_enabled=isinstance(context.hooks, HookEngine),
            memory_enabled=context.memory_store is not None,
            busy=self.busy,
            current_task_id=current.task_id if current is not None else "",
            turn_cap=config.turn_cap,
        )

    def task_views(self) -> list[TaskView]:
        return [
            _task_view(index, record)
            for index, record in enumerate(self._runtime.context.tasks, start=1)
        ]

    def history(self, limit: int) -> list[Mapping[str, object]]:
        """The last ``limit`` messages of the dialogue as ``{role, content}`` mappings."""

        messages = self._runtime.context.state.get("messages")
        if not isinstance(messages, list) or limit <= 0:
            return []
        return [_message_view(message) for message in messages[-limit:]]

    def clear_history(self) -> int:
        """Forget the dialogue carried across tasks; returns how many messages it held."""

        if self.busy:
            raise TaskBusyError("Cannot clear the history while a task is running.")
        state = self._runtime.context.state
        messages = state.get("messages")
        count = len(messages) if isinstance(messages, list) else 0
        state["messages"] = []
        self._runtime.context.persist()
        return count

    def memory_entries(self) -> list[MemoryEntry]:
        store = self._runtime.context.memory_store
        return list(store.entries()) if store is not None else []

    # ------------------------------------------------------------------ internals

    def _attach(self) -> None:
        """Bind approval tickets to the current session's tasks (once per session)."""

        context = self._runtime.context
        if self._attached is context:
            return
        self._attached = context
        broker = self._broker
        if broker is None:
            return

        def bind(_name: str, payload: object | None) -> None:
            if isinstance(payload, TaskRecord):
                broker.bind_task(payload.task_id, context.event_emitter)

        context.events.subscribe("task.started", bind)

    def _server_views(self) -> list[ServerView]:
        mcp = self._runtime.context.mcp
        if mcp is None:
            return []
        manager = mcp.manager
        counts: dict[str, int] = {}
        for tool in manager.list_tools():
            counts[tool.server] = counts.get(tool.server, 0) + 1
        views = []
        for name, status in mcp.status().items():
            views.append(
                ServerView(
                    name=name,
                    state=status.state.value,
                    generation=status.generation,
                    transport=str(manager.server_config(name).transport),
                    tool_count=counts.get(name, 0),
                    stateful=status.stateful,
                    error=status.last_error or "",
                    is_browser=name == mcp.browser_server,
                )
            )
        return views

    def _browser_state(self) -> str:
        if self._runtime.config.no_mcp:
            return "disabled"
        context = self._runtime.context
        mcp = context.mcp
        if not context.initialized or mcp is None:
            return "not started"
        if not mcp.browser_server:
            return "no browser server"
        status = mcp.status().get(mcp.browser_server)
        if status is None:
            return "unknown"
        if status.state is ConnectionState.READY:
            return "on"
        return status.state.value


def _task_view(index: int, record: TaskRecord) -> TaskView:
    status: TaskStatus = record.status if record.status in _TASK_STATUSES else "failed"  # type: ignore[assignment]
    return TaskView(
        index=index,
        task_id=record.task_id,
        task=record.task,
        status=status,
        started_at=record.started_at,
        finished_at=record.finished_at,
        turns=record.turns,
    )


def _message_view(message: Any) -> dict[str, object]:
    if isinstance(message, Message):
        return {"role": message.role, "content": str(message.content or "")}
    return {"role": "", "content": str(message)}


def _package_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return ""


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.info("Task ended with %s: %s", type(error).__name__, error)


__all__ = ["PACKAGE_NAME", "SessionController", "TaskBusyError"]
