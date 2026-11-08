"""Notifications: page data is sanitized and cut; a failing channel only logs an event."""

from __future__ import annotations

import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.serve.notify import TRUNCATED, LogChannel, Notification, Notifier
from tests.serve.serve_fakes import RecordingChannel


def note(**fields) -> Notification:
    return Notification(**{"kind": "change", "job_id": "j", "run_id": "run-1", "title": "j: changed", **fields})


@pytest.mark.asyncio
async def test_control_sequences_are_stripped_and_the_body_is_cut() -> None:
    channel = RecordingChannel()
    notifier = Notifier([channel], EventEmitter(InMemoryEventSink()), max_chars=100)

    delivered = await notifier.notify(note(title="\x1b[31mred\x1b[0m", body="x" * 500))

    sent, = channel.sent
    assert delivered == ("test",)
    assert sent.title == "red"
    assert len(sent.body) == 100 and sent.body.endswith(TRUNCATED)


@pytest.mark.asyncio
async def test_one_failing_channel_does_not_stop_the_others() -> None:
    sink = InMemoryEventSink()
    good = RecordingChannel()
    notifier = Notifier([RecordingChannel(fail=True), good], EventEmitter(sink), max_chars=100)

    delivered = await notifier.notify(note())

    assert delivered == ("test",)
    assert len(good.sent) == 1
    failed, = [r for r in sink.records if r.type == "serve.notify.failed"]
    assert failed.payload["error"] == "RuntimeError"
    assert "hunter2" not in str(failed.payload)


@pytest.mark.asyncio
async def test_the_log_channel_writes_an_event_and_a_line() -> None:
    sink = InMemoryEventSink()
    lines: list[str] = []

    await LogChannel(EventEmitter(sink), write=lines.append).send(note(body="Price 10 USD"))

    assert lines == ["[change] j: changed: Price 10 USD"]
    event, = sink.records
    assert (event.type, event.payload["kind"], event.payload["body"]) == ("serve.notified", "change", "Price 10 USD")
