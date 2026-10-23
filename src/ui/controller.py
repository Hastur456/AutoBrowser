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
from src.agent_loop.replay import load_events, print_action_sequence, summarize_trace
from src.browser.pages import current_page_url, page_url
from src.config import get_settings
from src.harness.session import SessionContext, SessionRuntime, TaskRecord
from src.harness.session_store import EVENTS_FILE_NAME, StoredSession, find_session, list_sessions
from src.mcp import ConnectionState
from src.messages import Message
from src.ui.views import (
    PastTask,
    ServerView,
    SessionView,
    StoredSessionView,
    TaskOutcome,
    TaskStatus,
    TaskView,
)
from src.ui.feed import sanitize_text
from src.version import package_version

if TYPE_CHECKING:
    from src.harness.session import AgentLoopResult

logger = logging.getLogger(__name__)

_OUTCOME_STATUSES: frozenset[str] = frozenset({"completed", "blocked", "cancelled", "failed"})

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
        #: The ``submit`` call while it starts the session (Chrome, MCP), before the task.
        self._starting: asyncio.Task[Any] | None = None
        #: ``reset``/``resume`` while they close one session and open another: nothing
        #: else may start in that window (a task would find no agent, a second switch
        #: would orphan a restored session and its lock).
        self._switching = False
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
        # Starting can take long (Chrome, MCP): run it as its own task so ``cancel()`` stops
        # the start alone, never the caller's task around ``submit``.
        starting = asyncio.ensure_future(self.start())
        self._starting = starting
        try:
            await asyncio.shield(starting)
        except asyncio.CancelledError:
            # The caller was cancelled: don't leave the start running behind it.
            starting.cancel()
            await asyncio.wait({starting})
            raise
        finally:
            self._starting = None
        if starting.cancelled():
            raise asyncio.CancelledError
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

    async def run_outcome(self, text: str) -> TaskOutcome:
        """Submit ``text``, wait for it and report how it ended (never raises for the task
        itself: a failure is a ``failed`` outcome). A session that cannot start still raises,
        and a cancellation propagates."""

        view = await self.submit(text)
        task = self._task
        assert task is not None
        try:
            result = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current is not None and current.cancelling()):
                # The task was cancelled (e.g. by ``cancel()``), not this caller.
                return TaskOutcome(task_id=view.task_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - reported as the failed outcome
            return TaskOutcome(task_id=view.task_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        record = next((r for r in self._runtime.context.tasks if r.task_id == view.task_id), None)
        status = record.status if record is not None else "failed"
        return TaskOutcome(
            task_id=view.task_id,
            status=status if status in _OUTCOME_STATUSES else "failed",  # type: ignore[arg-type]
            final_answer=str(result.final_answer or ""),
            turns=result.turns,
        )

    async def wait(self) -> TaskView | None:
        """Wait for the running task, if any, and return the last task's view."""

        task = self._task
        if task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
        views = self.task_views()
        return views[-1] if views else None

    async def cancel(self) -> bool:
        """Cancel the running task, or a ``submit`` still starting the session; ``False``
        when neither runs."""

        starting = self._starting
        if starting is not None and not starting.done():
            starting.cancel()
            # The session start cleans up after itself (Chrome, MCP) on cancellation.
            await asyncio.wait({starting})
            return True
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return True

    async def shutdown(self, timeout_seconds: float) -> None:
        """Cancel whatever runs and wait for it, at most ``timeout_seconds``, so the session
        is never closed under a running start or task."""

        running = [
            task
            for task in (self._starting, self._task)
            if task is not None and not task.done()
        ]
        for task in running:
            task.cancel()
        if not running:
            return
        _done, pending = await asyncio.wait(running, timeout=timeout_seconds)
        if pending:
            logger.warning(
                "%d task(s) did not stop within %.1fs of the shutdown", len(pending), timeout_seconds
            )

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

    async def available_models(self) -> list[str]:
        """The model ids the session's provider offers (one provider call, sorted).

        Raises when the session has no catalog or the provider cannot answer; the message
        is short and carries no secrets (``ModelProviderError``)."""

        return await self._runtime.list_models()

    async def set_model(self, model: str) -> SessionView:
        """Use ``model`` from the next task on, for this and the following sessions of the
        process (``/clear``, ``/resume``). Refuses while a task is running."""

        if self.busy:
            raise TaskBusyError("Cannot switch the model while a task is running.")
        await self._runtime.set_model(model)
        return self.session_view()

    async def reset(self) -> SessionView:
        """Close the session and continue in a fresh one (new id, no history, no tasks)."""

        if self.busy:
            raise TaskBusyError("Cannot reset while a task is running.")
        self._switching = True
        try:
            context = await self._runtime.reset()
            if self._broadcaster is not None:
                self._broadcaster.mark_reset(context.session_id)
            self._attach()
        finally:
            self._switching = False
        return self.session_view()

    async def resume(self, target: str) -> SessionView:
        """Switch to a stored session by list number (as :meth:`stored_sessions` orders
        them), id or id prefix, and continue it with its dialogue and task history.

        Raises :class:`TaskBusyError` while a task runs, ``LookupError`` for an unknown,
        current or not resumable session, ``SessionInUseError`` when another process has
        it open, ``ValueError`` for damaged files. The current session stays as it was on
        any refusal."""

        if self.busy:
            raise TaskBusyError("Cannot resume another session while a task is running.")
        self._switching = True
        try:
            session = await asyncio.to_thread(self._pick_session, target.strip())
            context = await self._runtime.switch_to(session.path)
            if self._broadcaster is not None:
                self._broadcaster.mark_reset(context.session_id)
            self._attach()
        finally:
            self._switching = False
        return self.session_view()

    def _pick_session(self, target: str) -> StoredSession:
        """A number within the offered list picks that entry (only an exact id beats it);
        anything else is an id or an unambiguous id prefix. Ids are hex, so a one-digit
        prefix is common: trying it first would make ``/resume 1`` depend on the ids."""

        sessions_dir = get_settings().storage.sessions_dir
        offered = self._resumable()
        exact = [session for session in offered if session.session_id == target]
        if exact:
            session = exact[0]
        elif target.isdigit() and 1 <= int(target) <= len(offered):
            session = offered[int(target) - 1]
        else:
            session = find_session(sessions_dir, target)
        if session.session_id == self._runtime.context.session_id:
            raise LookupError(f"Session {session.session_id[:12]} is the current one.")
        if not session.resumable:
            raise LookupError(f"Session {session.session_id[:12]} has nothing to resume.")
        return session

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
        """A task is running, or ``submit`` is still starting the session for one."""

        starting = self._starting is not None and not self._starting.done()
        running = self._task is not None and not self._task.done()
        return starting or running or self._switching

    def session_view(self) -> SessionView:
        context = self._runtime.context
        config = self._runtime.config
        workspace = context.workspace.root if context.workspace is not None else None
        current = context.tasks[-1] if self.busy and context.tasks else None
        return SessionView(
            session_id=context.session_id,
            version=package_version(),
            # The model id may come from a provider's list: never raw on a terminal.
            model=sanitize_text(config.model),
            provider=sanitize_text(config.provider),
            workspace=str(workspace) if workspace is not None else os.getcwd(),
            browser=self._browser_state(),
            servers=tuple(self._server_views()),
            permission_mode=context.permissions.mode,
            hooks_enabled=isinstance(context.hooks, HookEngine),
            memory_enabled=context.memory_store is not None,
            busy=self.busy,
            current_task_id=current.task_id if current is not None else "",
            turn_cap=config.turn_cap,
            resumed=context.resumed_from_sequence is not None,
            task_count=len(context.tasks),
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

    async def stored_sessions(self) -> list[StoredSessionView]:
        """Resumable stored sessions other than the current one, most recent first, at most
        ``ui.resume_list_size`` of them. The folders are read in a worker thread (task
        files can be large), never on the session loop."""

        sessions = await asyncio.to_thread(self._resumable)
        return [
            StoredSessionView(
                session_id=sanitize_text(session.session_id),
                last_activity=session.last_activity or session.started_at,
                task_count=session.task_count,
                last_task=sanitize_text(session.last_task),
            )
            for session in sessions
        ]

    def past_tasks(self) -> list[PastTask]:
        """The finished tasks of the current session, for replaying its conversation."""

        return [
            # From disk, like page content: no control sequence reaches a terminal.
            PastTask(
                index=index,
                task=sanitize_text(record.task),
                status=view.status,
                final_answer=sanitize_text(record.final_answer),
                turns=record.turns,
            )
            for index, record in enumerate(self._runtime.context.tasks, start=1)
            if (view := _task_view(index, record)).status != "running"
        ]

    def _resumable(self) -> list[StoredSession]:
        settings = get_settings()
        current = self._runtime.context.session_id
        sessions = [
            session
            for session in list_sessions(settings.storage.sessions_dir)
            if session.resumable and session.session_id != current
        ]
        return sessions[: settings.ui.resume_list_size]

    def current_url(self) -> str:
        """The page the agent last saw, read from the carried state by its ``Page URL``
        text format (no tool is called); ``""`` when unknown."""

        state = dict(self._runtime.context.state)
        return page_url(state.get("observation")) or current_page_url(state)

    async def replay(self, target: str = "") -> list[str]:
        """The action sequence of a task: the last one (``""``), the ``N``-th of this
        session, or a whole stored session by id or prefix. The trace is read in a worker
        thread, so a large file never stalls a running task on the session loop."""

        return await asyncio.to_thread(self._replay, target.strip())

    def _replay(self, target: str) -> list[str]:
        tasks = self._runtime.context.tasks
        # A number in the task range is a task; anything else (session ids are hex, so a
        # prefix may be all digits) is a stored session's id or prefix.
        is_task_number = target.isdigit() and 1 <= int(target) <= len(tasks)
        if target and not is_task_number:
            try:
                session = find_session(get_settings().storage.sessions_dir, target)
            except LookupError:
                if target.isdigit() and tasks:
                    raise LookupError(f"Task numbers run from 1 to {len(tasks)}.") from None
                raise
            trace = session.path / EVENTS_FILE_NAME
            if not trace.is_file():
                raise FileNotFoundError(f"Session {session.session_id[:12]} has no events yet.")
            events = load_events(trace)
            return [f"{session.session_id[:12]} · {summarize_trace(events).terminal_status}",
                    *print_action_sequence(events).splitlines()]
        if not tasks:
            raise LookupError("This session has no tasks yet.")
        index = int(target) if target else len(tasks)
        if not 1 <= index <= len(tasks):
            raise LookupError(f"Task numbers run from 1 to {len(tasks)}.")
        record = tasks[index - 1]
        session_dir = self._runtime.context.session_dir
        trace = session_dir / EVENTS_FILE_NAME if session_dir is not None else None
        if trace is None or not trace.is_file():
            raise FileNotFoundError("This session has no events yet.")
        events = [event for event in load_events(trace) if event.task_id == record.task_id]
        return [f"#{index} · {record.status}", *print_action_sequence(events).splitlines()]

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


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.info("Task ended with %s: %s", type(error).__name__, error)


__all__ = ["SessionController", "TaskBusyError"]
