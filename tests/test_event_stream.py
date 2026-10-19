"""The in-process event stream (:mod:`src.harness.event_stream`)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from src.agent_loop.events import REDACTED_VALUE, EventEmitter, EventRecord
from src.config import UiSettings
from src.harness.event_stream import EventBroadcaster, StreamCursor, StreamItem


def record(sequence: int, *, session_id: str = "s1", **payload: Any) -> EventRecord:
    return EventRecord(
        type="tool.finished",
        source="test",
        payload=payload,
        session_id=session_id,
        sequence=sequence,
    )


def broadcaster(**overrides: Any) -> EventBroadcaster:
    settings = {"event_buffer_size": 100, "subscriber_queue_size": 10, **overrides}
    return EventBroadcaster(UiSettings(**settings))


async def take(stream: Any, count: int) -> list[StreamItem]:
    items = []
    for _ in range(count):
        items.append(await asyncio.wait_for(anext(stream), timeout=2))
    return items


@pytest.mark.asyncio
async def test_a_late_subscriber_gets_the_buffer_then_live_events() -> None:
    stream_sink = broadcaster()
    stream_sink.emit(record(1))
    stream_sink.emit(record(2))
    stream = stream_sink.subscribe()

    first = await take(stream, 2)
    stream_sink.emit(record(3))
    live = await take(stream, 1)

    assert [item.kind for item in first + live] == ["event", "event", "event"]
    assert [item.cursor for item in first + live] == [
        StreamCursor("s1", 1),
        StreamCursor("s1", 2),
        StreamCursor("s1", 3),
    ]
    await stream.aclose()


@pytest.mark.asyncio
async def test_records_are_redacted_json_dicts() -> None:
    stream_sink = broadcaster()
    stream_sink.emit(record(1, authorization="Bearer x", text="ok"))

    (item,) = await take(stream_sink.subscribe(), 1)

    assert item.record is not None
    assert item.record["payload"] == {"authorization": REDACTED_VALUE, "text": "ok"}
    assert item.record["sequence"] == 1


@pytest.mark.asyncio
async def test_a_cursor_resumes_after_the_last_seen_event() -> None:
    stream_sink = broadcaster()
    for sequence in (1, 2, 3):
        stream_sink.emit(record(sequence))

    items = await take(stream_sink.subscribe(after=StreamCursor("s1", 2)), 1)

    assert [item.cursor.sequence for item in items] == [3]


@pytest.mark.asyncio
async def test_a_cursor_older_than_the_buffer_yields_a_gap_first() -> None:
    stream_sink = broadcaster(event_buffer_size=100)
    for sequence in range(1, 106):
        stream_sink.emit(record(sequence))

    items = await take(stream_sink.subscribe(after=StreamCursor("s1", 1)), 2)

    assert items[0].kind == "gap"
    assert items[0].cursor == StreamCursor("s1", 5)
    assert items[1].cursor.sequence == 6


@pytest.mark.asyncio
async def test_a_new_session_resets_the_buffer_and_the_subscribers() -> None:
    stream_sink = broadcaster()
    stream_sink.emit(record(1, session_id="old"))
    stream = stream_sink.subscribe()
    await take(stream, 1)

    stream_sink.emit(record(1, session_id="new"))
    items = await take(stream, 2)

    assert [item.kind for item in items] == ["reset", "event"]
    assert items[0].cursor == StreamCursor("new", 0)
    assert stream_sink.session_id == "new"
    stale = await take(stream_sink.subscribe(after=StreamCursor("old", 1)), 2)
    assert [item.kind for item in stale] == ["reset", "event"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_slow_subscriber_gets_a_gap_instead_of_blocking() -> None:
    stream_sink = broadcaster(subscriber_queue_size=10)
    stream = stream_sink.subscribe()
    stream_sink.emit(record(1))
    await take(stream, 1)  # registered and drained

    for sequence in range(2, 30):
        stream_sink.emit(record(sequence))
    await asyncio.sleep(0.05)  # let the deliveries run
    items = []
    while True:
        try:
            items.append(await asyncio.wait_for(anext(stream), timeout=0.1))
        except TimeoutError:
            break

    assert "gap" in [item.kind for item in items]
    assert len(items) <= 10
    assert items[-1].cursor.sequence == 29
    await stream.aclose()


@pytest.mark.asyncio
async def test_events_emitted_on_another_thread_reach_the_subscriber() -> None:
    stream_sink = broadcaster()
    stream = stream_sink.subscribe()
    stream_sink.emit(record(1))
    await take(stream, 1)

    emitter = EventEmitter(stream_sink, session_id="s1")
    thread = threading.Thread(
        target=lambda: emitter.emit("goal.started", source="test", payload={"task": "x"})
    )
    thread.start()
    thread.join()
    (item,) = await take(stream, 1)

    assert item.record is not None and item.record["type"] == "goal.started"
    await stream.aclose()


@pytest.mark.asyncio
async def test_closing_a_subscription_unregisters_it() -> None:
    stream_sink = broadcaster()
    stream_sink.emit(record(1))
    stream = stream_sink.subscribe()
    await take(stream, 1)

    await stream.aclose()
    stream_sink.emit(record(2))

    assert stream_sink._state.subscribers == []
