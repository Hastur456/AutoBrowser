"""The session's approval broker (:mod:`src.harness.approvals`)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from src.agent_loop.events import REDACTED_VALUE, EventEmitter, InMemoryEventSink
from src.config import PermissionsSettings
from src.contracts import ApprovalResolution, ApprovalTicket, PermissionVerdict
from src.harness.approvals import ApprovalBroker

REQUEST = {"name": "browser_click", "args": {"element": "Купить", "api_key": "secret"}}
GRANTABLE = PermissionVerdict(
    decision="ask",
    reason="Approval required by rule buy.",
    source="rule",
    rule_id="buy",
    grant_key=("playwright", "browser_click", "ozon.ru"),
)
ALWAYS = PermissionVerdict(
    decision="ask", reason="Needs approval.", source="classifier", always_ask=True
)


def make_broker(**overrides: Any) -> tuple[ApprovalBroker, InMemoryEventSink]:
    sink = InMemoryEventSink()
    settings = PermissionsSettings(**overrides.pop("settings", {}))
    broker = ApprovalBroker(
        settings,
        progress_timeout_seconds=overrides.pop("progress_timeout_seconds", 10.0),
        events=EventEmitter(sink, session_id="s1"),
        **overrides,
    )
    broker.bind_task("task-1")
    return broker, sink


async def wait_for_ticket(broker: ApprovalBroker) -> ApprovalTicket:
    for _ in range(400):
        pending = broker.pending()
        if pending:
            return pending[0]
        await asyncio.sleep(0.005)
    raise AssertionError("no ticket")


@pytest.mark.asyncio
async def test_a_ticket_describes_the_call_without_raw_secrets() -> None:
    broker, _ = make_broker()
    seen: list[Any] = []
    broker.subscribe(seen.append)

    task = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    ticket = await wait_for_ticket(broker)
    assert broker.answer(ticket.ticket_id, "once", via="tui") is True
    assert await task == "once"

    assert ticket.task_id == "task-1"
    assert (ticket.tool, ticket.server, ticket.domain) == ("browser_click", "playwright", "ozon.ru")
    assert (ticket.rule_id, ticket.source, ticket.allow_session) == ("buy", "rule", True)
    assert "Купить" in ticket.args_preview
    assert "secret" not in ticket.args_preview
    assert REDACTED_VALUE in ticket.args_preview
    assert (ticket.deadline - ticket.created_at).total_seconds() == pytest.approx(8.0)
    assert seen[0] == ticket
    assert isinstance(seen[1], ApprovalResolution)
    assert (seen[1].answer, seen[1].via) == ("once", "tui")
    assert broker.pending() == ()


@pytest.mark.asyncio
async def test_the_answer_is_emitted_as_approval_answered() -> None:
    broker, sink = make_broker()

    task = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    ticket = await wait_for_ticket(broker)
    broker.answer(ticket.ticket_id, "session", via="plain")
    await task

    (record,) = [record for record in sink.records if record.type == "approval.answered"]
    assert record.payload == {"ticket_id": ticket.ticket_id, "answer": "session", "via": "plain"}
    assert record.task_id == "task-1"


@pytest.mark.asyncio
async def test_the_first_answer_wins_and_late_ones_are_refused() -> None:
    broker, _ = make_broker()

    task = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    ticket = await wait_for_ticket(broker)
    assert broker.answer(ticket.ticket_id, "deny", via="tui") is True
    assert broker.answer(ticket.ticket_id, "once", via="web") is False
    assert await task == "deny"
    assert broker.answer(ticket.ticket_id, "once", via="tui") is False
    assert broker.answer("unknown", "once", via="tui") is False


@pytest.mark.asyncio
async def test_session_is_refused_for_an_always_ask_ticket() -> None:
    broker, _ = make_broker()

    task = asyncio.create_task(broker(REQUEST, ALWAYS.reason, ALWAYS))
    ticket = await wait_for_ticket(broker)
    assert ticket.allow_session is False
    assert broker.answer(ticket.ticket_id, "session", via="tui") is False
    assert broker.answer(ticket.ticket_id, "once", via="tui") is True
    assert await task == "once"


@pytest.mark.asyncio
async def test_no_answer_in_time_is_a_deny_via_timeout() -> None:
    broker, sink = make_broker(timeout_seconds=0.05)
    seen: list[Any] = []
    broker.subscribe(seen.append)

    assert await broker(REQUEST, GRANTABLE.reason, GRANTABLE) == "deny"

    assert isinstance(seen[-1], ApprovalResolution)
    assert seen[-1].via == "timeout"
    assert sink.records[-1].payload["via"] == "timeout"
    assert broker.pending() == ()


def test_the_timeout_is_the_configured_share_of_the_progress_timeout() -> None:
    broker, _ = make_broker(
        settings={"approval_timeout_ratio": 0.5}, progress_timeout_seconds=30.0
    )

    assert broker.timeout_seconds == 15.0


@pytest.mark.asyncio
async def test_an_answer_from_another_thread_resolves_the_wait() -> None:
    broker, _ = make_broker()

    task = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    ticket = await wait_for_ticket(broker)
    results: list[bool] = []
    thread = threading.Thread(
        target=lambda: results.append(
            broker.answer_threadsafe(ticket.ticket_id, "once", via="tui").result()
        )
    )
    thread.start()
    thread.join()

    assert results == [True]
    assert await task == "once"


@pytest.mark.asyncio
async def test_a_failing_listener_does_not_break_the_approval() -> None:
    broker, _ = make_broker()

    def broken(_item: Any) -> None:
        raise RuntimeError("ui crashed")

    broker.subscribe(broken)
    unsubscribe_seen: list[Any] = []
    unsubscribe = broker.subscribe(unsubscribe_seen.append)
    unsubscribe()

    task = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    ticket = await wait_for_ticket(broker)
    broker.answer(ticket.ticket_id, "once", via="tui")

    assert await task == "once"
    assert unsubscribe_seen == []


@pytest.mark.asyncio
async def test_a_cancelled_wait_still_resolves_the_ticket_for_every_front_end() -> None:
    broker, sink = make_broker()
    seen: list[Any] = []
    broker.subscribe(seen.append)

    waiting = asyncio.create_task(broker(REQUEST, GRANTABLE.reason, GRANTABLE))
    await wait_for_ticket(broker)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert isinstance(seen[-1], ApprovalResolution)
    assert (seen[-1].answer, seen[-1].via) == ("deny", "cancelled")
    assert sink.records[-1].type == "approval.answered"
    assert sink.records[-1].payload["via"] == "cancelled"
    assert broker.pending() == ()
