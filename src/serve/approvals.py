"""``serve.api_approvals``: approvals without a terminal.

Each new ticket of the worker's ``ApprovalBroker`` goes to every channel as an ``approval``
notification (whatever the job's ``notify_on``: someone has to answer it); the answer comes
back through ``POST /v1/approvals/{id}`` (``via="api"``). Unanswered tickets time out to
``deny`` in the broker, as everywhere else.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.contracts import ApprovalTicket
from src.serve.dispatcher import Dispatcher
from src.serve.notify import Notification, Notifier
from src.serve.worker import WorkerPort

_LOG = logging.getLogger(__name__)


class ApprovalBridge:
    def __init__(self, worker: WorkerPort, notifier: Notifier, dispatcher: Dispatcher) -> None:
        self._worker = worker
        self._notifier = notifier
        self._dispatcher = dispatcher
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

    def answer(self, ticket_id: str, answer: str) -> bool:
        return self._worker.answer(ticket_id, answer, via="api")

    def _on_item(self, item: Any) -> None:
        # Runs on the session loop, which is the stand's loop: schedule the notification.
        if not isinstance(item, ApprovalTicket):
            return
        task = asyncio.get_running_loop().create_task(self._send(item))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, ticket: ApprovalTicket) -> None:
        job_id, run_id = self._dispatcher.running() or ("", "")
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
                )
            )
        except Exception:  # noqa: BLE001 - a notification never breaks the approval
            _LOG.warning("the approval notification of %s failed", ticket.ticket_id, exc_info=True)


__all__ = ["ApprovalBridge"]
