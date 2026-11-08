"""api_approvals: tickets go out as notifications and are answered through the API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.contracts import ApprovalTicket
from src.serve.approvals import ApprovalBridge
from tests.serve.serve_fakes import Rig


def ticket(ticket_id: str = "tk-1") -> ApprovalTicket:
    now = datetime(2026, 11, 4, 9, tzinfo=UTC)
    return ApprovalTicket(
        ticket_id=ticket_id, task_id="task-1", tool="browser_click", server="playwright",
        domain="shop.example", reason="Approval required by rule buy.", rule_id="buy", source="rule",
        args_preview='{"element": "Buy"}', allow_session=False, created_at=now,
        deadline=now + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_a_new_ticket_is_sent_to_the_channels(tmp_path: Path) -> None:
    rig = Rig(tmp_path, api_approvals=True)
    bridge = ApprovalBridge(rig.worker, rig.dispatcher.notifier, rig.dispatcher)
    bridge.start()

    rig.worker.approval_listeners[0](ticket())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    sent, = rig.channel.sent
    assert (sent.kind, sent.ticket_id) == ("approval", "tk-1")
    assert "browser_click" in sent.title and "shop.example" in sent.title
    assert "Approval required by rule buy." in sent.body


def test_pending_tickets_and_answers_go_through_the_worker(tmp_path: Path) -> None:
    rig = Rig(tmp_path, api_approvals=True)
    rig.worker.tickets = [ticket()]
    bridge = ApprovalBridge(rig.worker, rig.dispatcher.notifier, rig.dispatcher)

    pending, = bridge.pending()
    answered = bridge.answer("tk-1", "once")

    assert pending["ticket_id"] == "tk-1" and pending["allow_session"] is False
    assert "args_preview" in pending
    assert answered is True
    assert rig.worker.answers == [("tk-1", "once", "api")]
