"""Approval broker: permission ``ask`` verdicts answered by whichever front end is attached.

:class:`ApprovalBroker` is the session's
:data:`~src.agent_loop.execution.loop.HumanInputCallback`. Each call becomes an
:class:`~src.contracts.ApprovalTicket` that subscribers (a terminal UI, the plain front end,
the cmd2 shell, later a web client) render; the first valid :meth:`ApprovalBroker.answer`
wins, from any thread. The wait is bounded by ``permissions.approval_timeout_ratio`` of
``loop.progress_timeout_seconds`` -- the goal watchdog sees no events while a human thinks
-- and no answer in time is a ``deny`` (``via="timeout"``).

Listeners are called on the session loop's thread (where the engine awaits the callback);
a UI marshals them onto its own loop. A failing listener never breaks the approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.agent_loop.events import EventEmitter, redact_json_safe
from src.config import PermissionsSettings
from src.contracts import (
    ApprovalAnswer,
    ApprovalResolution,
    ApprovalTicket,
    ApprovalVia,
    PermissionVerdict,
    ToolRequest,
)

logger = logging.getLogger(__name__)

ApprovalListener = Callable[[ApprovalTicket | ApprovalResolution], None]


@dataclass
class _Pending:
    ticket: ApprovalTicket
    future: Future[tuple[ApprovalAnswer, ApprovalVia]]


class ApprovalBroker:
    """Session-scoped :data:`HumanInputCallback` backed by tickets and subscribers."""

    def __init__(
        self,
        settings: PermissionsSettings,
        *,
        progress_timeout_seconds: float,
        events: EventEmitter | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        #: ``timeout_seconds`` overrides the derived wait (tests, explicit front ends).
        self._timeout = (
            progress_timeout_seconds * settings.approval_timeout_ratio
            if timeout_seconds is None
            else timeout_seconds
        )
        self._events = events
        self._task_id = ""
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._listeners: list[ApprovalListener] = []

    @property
    def timeout_seconds(self) -> float:
        """How long one ticket waits before it is denied."""

        return self._timeout

    def bind_task(self, task_id: str, events: EventEmitter | None = None) -> None:
        """Tickets from now on belong to ``task_id``; ``events`` replaces the emitter."""

        self._task_id = task_id
        if events is not None:
            self._events = events

    async def __call__(
        self,
        request: ToolRequest,
        reason: str,
        verdict: PermissionVerdict,
    ) -> ApprovalAnswer:
        now = datetime.now(UTC)
        ticket = ApprovalTicket(
            ticket_id=f"approval-{uuid4().hex}",
            task_id=self._task_id,
            tool=str(request.get("name") or ""),
            server=verdict.grant_key[0] if verdict.grant_key else "",
            domain=verdict.grant_key[2] if verdict.grant_key else "",
            reason=reason,
            rule_id=verdict.rule_id,
            source=verdict.source,
            args_preview=self._args_preview(request),
            allow_session=verdict.grant_key is not None and not verdict.always_ask,
            created_at=now,
            deadline=now + timedelta(seconds=self._timeout),
        )
        pending = _Pending(ticket=ticket, future=Future())
        with self._lock:
            self._pending[ticket.ticket_id] = pending
        self._notify(ticket)
        try:
            answer, via = await asyncio.wait_for(
                asyncio.wrap_future(pending.future), self._timeout
            )
        except TimeoutError:
            answer, via = "deny", "timeout"
        finally:
            with self._lock:
                self._pending.pop(ticket.ticket_id, None)
            if not pending.future.done():
                pending.future.cancel()
        resolution = ApprovalResolution(
            ticket_id=ticket.ticket_id,
            answer=answer,
            via=via,
            resolved_at=datetime.now(UTC),
        )
        self._emit_answered(ticket, resolution)
        self._notify(resolution)
        return answer

    def pending(self) -> tuple[ApprovalTicket, ...]:
        """Tickets waiting for an answer, oldest first."""

        with self._lock:
            return tuple(item.ticket for item in self._pending.values())

    def answer(self, ticket_id: str, answer: ApprovalAnswer, *, via: ApprovalVia) -> bool:
        """Answer a pending ticket from any thread; ``False`` when it is not answerable.

        Not answerable: unknown or already answered ticket, or ``session`` for a ticket
        that does not allow a session grant.
        """

        with self._lock:
            pending = self._pending.get(ticket_id)
            if pending is None or pending.future.done():
                return False
            if answer == "session" and not pending.ticket.allow_session:
                return False
            try:
                pending.future.set_result((answer, via))
            except Exception:  # cancelled concurrently by the timeout
                return False
        return True

    def answer_threadsafe(
        self, ticket_id: str, answer: ApprovalAnswer, *, via: ApprovalVia
    ) -> Future[bool]:
        """:meth:`answer` as an already completed future, for callers that expect one."""

        result: Future[bool] = Future()
        result.set_result(self.answer(ticket_id, answer, via=via))
        return result

    def subscribe(self, listener: ApprovalListener) -> Callable[[], None]:
        """Call ``listener`` with every new ticket and resolution; returns the unsubscribe."""

        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _args_preview(self, request: ToolRequest) -> str:
        args = redact_json_safe(request.get("args") or {})
        try:
            text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(args)
        limit = self._settings.approval_args_preview_chars
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return text

    def _notify(self, item: ApprovalTicket | ApprovalResolution) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(item)
            except Exception:
                logger.exception("Approval listener failed")

    def _emit_answered(self, ticket: ApprovalTicket, resolution: ApprovalResolution) -> None:
        if self._events is None:
            return
        self._events.emit(
            "approval.answered",
            source="harness.approvals",
            payload={
                "ticket_id": resolution.ticket_id,
                "answer": resolution.answer,
                "via": resolution.via,
            },
            task_id=ticket.task_id or None,
            goal_id=ticket.task_id or None,
        )


__all__ = ["ApprovalBroker", "ApprovalListener"]
