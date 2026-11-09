"""Approvals without a terminal (``serve.api_approvals``, a chat channel's buttons).

Each new ticket of a worker's ``ApprovalBroker`` goes to every channel as an ``approval``
notification (whatever the job's ``notify_on``: someone has to answer it); the answer comes
back through ``POST /v1/approvals/{id}`` (``via="api"``) or a chat channel (``via="channel"``).
Unanswered tickets time out to ``deny`` in the broker, as everywhere else. The
:class:`ApprovalDesk` puts the bridges of every worker behind one :class:`ApprovalsPort`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from src.contracts import ApprovalAnswer, ApprovalTicket, ApprovalVia
from src.serve.dispatcher import Dispatcher
from src.serve.notify import Notification, Notifier
from src.serve.worker import WorkerPort

_LOG = logging.getLogger(__name__)


#: What a ticket may be answered with: ``session`` only when the ticket allows it.
_ANSWERS: tuple[ApprovalAnswer, ...] = ("once", "deny")
_SESSION_ANSWERS: tuple[ApprovalAnswer, ...] = ("once", "session", "deny")


class ApprovalsPort(Protocol):
    """Pending approvals and how to answer one (the API, a chat channel)."""

    def pending(self) -> list[dict[str, Any]]: ...

    def answer(self, ticket_id: str, answer: str, *, via: ApprovalVia = "api") -> bool: ...


class ApprovalBridge:
    """The approvals of one worker (``index`` in the pool): its tickets go out as
    notifications naming the run of that worker."""

    def __init__(
        self, worker: WorkerPort, notifier: Notifier, dispatcher: Dispatcher, *, index: int = 0
    ) -> None:
        self._worker = worker
        self._notifier = notifier
        self._dispatcher = dispatcher
        self._index = index
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unsubscribe = lambda: None

    def start(self) -> None:
        self._unsubscribe = self._worker.subscribe_approvals(self._on_item)

    def stop(self) -> None:
        self._unsubscribe()

    def pending(self) -> list[dict[str, Any]]:
        return [
            {
                "ticket_id": ticket.ticket_id,
                "task_id": ticket.task_id,
                "tool": ticket.tool,
                "server": ticket.server,
                "domain": ticket.domain,
                "reason": ticket.reason,
                "rule_id": ticket.rule_id,
                "args_preview": ticket.args_preview,
                "allow_session": ticket.allow_session,
                "deadline": ticket.deadline.isoformat(),
            }
            for ticket in self._worker.pending_approvals()
        ]

    def holds(self, ticket_id: str) -> bool:
        """The ticket is open on this bridge's worker."""

        return any(ticket.ticket_id == ticket_id for ticket in self._worker.pending_approvals())

    def answer(self, ticket_id: str, answer: str, *, via: ApprovalVia = "api") -> bool:
        return self._worker.answer(ticket_id, answer, via=via)

    def _on_item(self, item: Any) -> None:
        # Runs on the session loop, which is the stand's loop: schedule the notification.
        if not isinstance(item, ApprovalTicket):
            return
        task = asyncio.get_running_loop().create_task(self._send(item))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, ticket: ApprovalTicket) -> None:
        job_id, run_id = self._dispatcher.running_on(self._index) or ("", "")
        where = f" on {ticket.domain}" if ticket.domain else ""
        try:
            await self._notifier.notify(
                Notification(
                    kind="approval",
                    job_id=job_id,
                    run_id=run_id,
                    title=f"Approval needed: {ticket.tool}{where}",
                    body=f"{ticket.reason}\n{ticket.args_preview}\nanswer by {ticket.deadline.isoformat()}",
                    ticket_id=ticket.ticket_id,
                    answers=_SESSION_ANSWERS if ticket.allow_session else _ANSWERS,
                )
            )
        except Exception:  # noqa: BLE001 - a notification never breaks the approval
            _LOG.warning("the approval notification of %s failed", ticket.ticket_id, exc_info=True)


class ApprovalDesk:
    """:class:`ApprovalsPort` over every worker's bridge: the pending tickets of all of them;
    an answer goes to the worker that holds the ticket (ids are unique across workers)."""

    def __init__(self, bridges: Sequence[ApprovalBridge]) -> None:
        self._bridges = list(bridges)

    def pending(self) -> list[dict[str, Any]]:
        return [item for bridge in self._bridges for item in bridge.pending()]

    def answer(self, ticket_id: str, answer: str, *, via: ApprovalVia = "api") -> bool:
        for bridge in self._bridges:
            if bridge.holds(ticket_id):
                return bridge.answer(ticket_id, answer, via=via)
        return False


__all__ = ["ApprovalBridge", "ApprovalDesk", "ApprovalsPort"]
