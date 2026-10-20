"""The session surface the terminal UI drives, as structural protocols.

:class:`src.ui.controller.SessionController` satisfies :class:`ControllerPort`; tests use a
fake. The TUI calls every method on the session's runtime loop (through
:class:`RuntimeBridge`), never on the UI loop, so the controller needs no locking.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future
from typing import Any, Protocol, TypeVar

from src.contracts import MemoryEntry
from src.ui.tui.transcript import ApprovalResolutionView, ApprovalTicketView
from src.ui.views import PastTask, ServerView, SessionView, StoredSessionView, TaskView

T = TypeVar("T")


class StreamItemView(Protocol):
    """``src.harness.event_stream.StreamItem``: ``event``/``gap``/``reset`` at a cursor."""

    @property
    def kind(self) -> str: ...
    @property
    def cursor(self) -> Any: ...
    @property
    def record(self) -> Mapping[str, object] | None: ...


ApprovalListener = Callable[[ApprovalTicketView | ApprovalResolutionView], None]


class ControllerPort(Protocol):
    async def start(self) -> SessionView: ...
    async def submit(self, text: str) -> TaskView: ...
    async def cancel(self) -> bool: ...
    async def reconnect(self, server: str) -> ServerView: ...
    async def reset(self) -> SessionView: ...
    def answer(self, ticket_id: str, answer: str, *, via: str) -> bool: ...
    def session_view(self) -> SessionView: ...
    def task_views(self) -> list[TaskView]: ...
    def history(self, limit: int) -> Sequence[Mapping[str, object]]: ...
    def memory_entries(self) -> Sequence[MemoryEntry]: ...
    def current_url(self) -> str: ...
    async def resume(self, target: str) -> SessionView: ...
    def stored_sessions(self) -> list[StoredSessionView]: ...
    def past_tasks(self) -> list[PastTask]: ...
    async def replay(self, target: str = "") -> list[str]: ...
    def subscribe(self, after: Any = None) -> AsyncIterator[StreamItemView]: ...
    def subscribe_approvals(self, listener: ApprovalListener) -> Callable[[], None]: ...


class RuntimePort(Protocol):
    """``src.ui.runtime_loop.RuntimeLoop``: the thread with the session's event loop."""

    def submit(self, coro: Awaitable[T]) -> Future[T]: ...


class RuntimeBridge:
    """Run controller calls on the runtime loop and await them from the UI loop."""

    def __init__(self, runtime: RuntimePort) -> None:
        self._runtime = runtime

    async def run(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Await ``coro_factory()`` executed on the runtime loop."""

        async def call() -> T:
            return await coro_factory()

        return await asyncio.wrap_future(self._runtime.submit(call()))

    async def call(self, fn: Callable[[], T]) -> T:
        """Await the synchronous ``fn()`` executed on the runtime loop."""

        async def call() -> T:
            return fn()

        return await asyncio.wrap_future(self._runtime.submit(call()))

    def spawn(self, coro_factory: Callable[[], Awaitable[T]]) -> Future[T]:
        """Start ``coro_factory()`` on the runtime loop without waiting for it."""

        async def call() -> T:
            return await coro_factory()

        return self._runtime.submit(call())


__all__ = [
    "ApprovalListener",
    "ControllerPort",
    "RuntimeBridge",
    "RuntimePort",
    "StreamItemView",
]
