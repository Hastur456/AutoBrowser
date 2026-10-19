"""In-process event stream: a session-scoped sink that fans redacted events out to clients.

The durable sinks (``events.jsonl``, ``agent_trace.jsonl``) are files; a terminal or web UI
needs the same events live. :class:`EventBroadcaster` is one more :class:`EventSink` of the
session's ``CompositeEventSink``: it keeps the last ``ui.event_buffer_size`` records (already
redacted through :meth:`EventRecord.to_json_dict`, never the raw payload) and pushes each
new one to every subscriber.

Threading: ``emit`` runs on whatever thread emits (the session loop); each subscriber is an
async iterator bound to the event loop it was created on, fed through
``call_soon_threadsafe``. A subscriber that falls more than ``ui.subscriber_queue_size``
items behind loses its backlog and gets one ``gap`` item instead: the stream never blocks
the agent. A record of another session than the current one (after a session reset) clears
the buffer and sends every subscriber a ``reset`` item first.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.agent_loop.events import EventRecord
from src.config import UiSettings

StreamItemKind = Literal["event", "gap", "reset"]


@dataclass(frozen=True)
class StreamCursor:
    """Position in the stream: the session and the ``EventRecord.sequence`` of an event."""

    session_id: str
    sequence: int


@dataclass(frozen=True)
class StreamItem:
    """One item of a subscription.

    ``event``: ``record`` is the redacted ``EventRecord.to_json_dict()`` and ``cursor`` its
    position. ``gap``: events up to ``cursor`` were dropped for this subscriber (re-subscribe
    with ``after=cursor`` to replay what the buffer still holds). ``reset``: a new session
    started at ``cursor``; everything shown before belongs to the old one.
    """

    kind: StreamItemKind
    cursor: StreamCursor
    record: Mapping[str, object] | None = None


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[StreamItem]
    limit: int
    closed: bool = False

    def offer(self, item: StreamItem) -> None:
        """Runs on the subscriber's loop."""

        if self.closed:
            return
        if self.queue.qsize() >= self.limit:
            # Overflow: drop the backlog; the gap says how far the loss goes.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(StreamItem(kind="gap", cursor=item.cursor))
            return
        self.queue.put_nowait(item)


@dataclass
class _State:
    session_id: str = ""
    buffer: deque[StreamItem] = field(default_factory=deque)
    subscribers: list[_Subscriber] = field(default_factory=list)


class EventBroadcaster:
    """:class:`~src.agent_loop.events.EventSink` that streams redacted events to clients."""

    def __init__(self, settings: UiSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._state = _State(buffer=deque(maxlen=settings.event_buffer_size))

    @property
    def session_id(self) -> str:
        """The session the buffered events belong to ("" before the first event)."""

        with self._lock:
            return self._state.session_id

    def emit(self, record: EventRecord) -> None:
        session_id = record.session_id or ""
        item = StreamItem(
            kind="event",
            cursor=StreamCursor(session_id, record.sequence),
            record=record.to_json_dict(),
        )
        with self._lock:
            reset = self._switch_session(session_id)
            self._state.buffer.append(item)
            subscribers = list(self._state.subscribers)
        for subscriber in subscribers:
            if reset is not None:
                self._deliver(subscriber, reset)
            self._deliver(subscriber, item)

    def mark_reset(self, session_id: str) -> None:
        """Start a new session explicitly (before its first event arrives)."""

        with self._lock:
            reset = self._switch_session(session_id)
            subscribers = list(self._state.subscribers)
        if reset is not None:
            for subscriber in subscribers:
                self._deliver(subscriber, reset)

    async def subscribe(self, after: StreamCursor | None = None) -> AsyncIterator[StreamItem]:
        """Yield buffered events after ``after`` (all of them for ``None``), then live ones.

        A cursor of another session yields a ``reset`` first and then the whole buffer; a
        cursor older than the buffer yields a ``gap`` before what is still kept.
        """

        loop = asyncio.get_running_loop()
        subscriber = _Subscriber(
            loop=loop,
            queue=asyncio.Queue(),
            limit=self._settings.subscriber_queue_size,
        )
        with self._lock:
            backlog = self._backlog(after)
            self._state.subscribers.append(subscriber)
        try:
            for item in backlog:
                yield item
            while True:
                yield await subscriber.queue.get()
        finally:
            subscriber.closed = True
            with self._lock:
                if subscriber in self._state.subscribers:
                    self._state.subscribers.remove(subscriber)

    def _switch_session(self, session_id: str) -> StreamItem | None:
        """Under the lock: adopt ``session_id``; a change clears the buffer, returns ``reset``."""

        state = self._state
        if session_id == state.session_id:
            return None
        previous = state.session_id
        state.session_id = session_id
        state.buffer.clear()
        if not previous:
            return None  # the first session is not a reset
        return StreamItem(kind="reset", cursor=StreamCursor(session_id, 0))

    def _backlog(self, after: StreamCursor | None) -> list[StreamItem]:
        """Under the lock: what a new subscriber gets before the live events."""

        state = self._state
        buffered = list(state.buffer)
        if after is None:
            return buffered
        if after.session_id != state.session_id:
            return [StreamItem(kind="reset", cursor=StreamCursor(state.session_id, 0)), *buffered]
        newer = [item for item in buffered if item.cursor.sequence > after.sequence]
        oldest = buffered[0].cursor.sequence if buffered else after.sequence + 1
        if oldest > after.sequence + 1:
            return [StreamItem(kind="gap", cursor=StreamCursor(state.session_id, oldest - 1)), *newer]
        return newer

    def _deliver(self, subscriber: _Subscriber, item: StreamItem) -> None:
        try:
            subscriber.loop.call_soon_threadsafe(subscriber.offer, item)
        except RuntimeError:  # the subscriber's loop is closed: it will never read again
            subscriber.closed = True
            with self._lock:
                if subscriber in self._state.subscribers:
                    self._state.subscribers.remove(subscriber)


__all__ = ["EventBroadcaster", "StreamCursor", "StreamItem", "StreamItemKind"]
