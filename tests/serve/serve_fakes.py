"""Fakes for the stand: a worker in place of SessionController and a clock that jumps."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.config import ServeSettings
from src.contracts import RunProfile, TaskUsage
from src.serve.dispatcher import Dispatcher
from src.serve.ledger import Ledger
from src.serve.notify import Notification, Notifier
from src.serve.state import JobStateStore
from src.serve.tally import RunTally
from src.ui.views import ServerView, SessionView, TaskOutcome

START = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


class FakeClock:
    """Aware time that only moves when told to, or when someone sleeps (kept in UTC, so a
    jump across a DST change is real elapsed time, not wall-clock arithmetic)."""

    def __init__(self, now: datetime = START) -> None:
        self.current = now.astimezone(UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


def outcome(status: str = "completed", *, answer: str = "done", stop_reason: str = "answered",
            stage: str = "agent", calls: int = 1, tokens: int = 100, cost: float | None = 0.01,
            task_id: str = "") -> Callable[[str, RunProfile], TaskOutcome]:
    """A scripted run_outcome result."""

    def make(text: str, profile: RunProfile) -> TaskOutcome:
        return TaskOutcome(
            task_id=task_id or f"task-{abs(hash((text, id(profile)))) % 10_000}",
            status=status,  # type: ignore[arg-type]
            final_answer=answer,
            stop_reason=stop_reason,
            stage=stage,
            usage=TaskUsage(model_calls=calls, input_tokens=tokens // 2,
                            output_tokens=tokens - tokens // 2, cost=cost),
        )

    return make


class FakeWorker:
    """WorkerPort over scripted outcomes; ``gate`` holds a run until the test opens it."""

    def __init__(self, *results: Callable[[str, RunProfile], TaskOutcome], tools: tuple[str, ...] = (),
                 recordings: tuple[str, ...] = ()) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, RunProfile]] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.servers: tuple[ServerView, ...] = ()
        self.reconnected: list[str] = []
        self.reconnect_fails = False
        self.resets = 0
        self.cancelled = 0
        self.tools = tools
        self.recordings = set(recordings)
        self.answers: list[tuple[str, str, str]] = []
        self.on_run: Callable[[str, RunProfile, TaskOutcome], None] | None = None
        self.approval_listeners: list[Callable[[Any], None]] = []
        self.tickets: list[Any] = []

    async def start(self) -> SessionView:
        return self.session_view()

    async def run_outcome(self, text: str, *, profile: RunProfile | None = None) -> TaskOutcome:
        profile = profile or RunProfile()
        self.calls.append((text, profile))
        self.started.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                return TaskOutcome(task_id="task-cancelled", status="cancelled")
        make = self.results.pop(0) if self.results else outcome()
        result = make(text, profile)
        if self.on_run is not None:
            self.on_run(text, profile, result)
        return result

    async def cancel(self) -> bool:
        self.cancelled += 1
        return True

    async def reset(self) -> SessionView:
        self.resets += 1
        self.servers = ()
        return self.session_view()

    async def reconnect(self, server: str) -> ServerView:
        self.reconnected.append(server)
        if self.reconnect_fails:
            raise RuntimeError("cannot reconnect")
        self.servers = tuple(ServerView(name=s.name, state="ready") for s in self.servers)
        return self.servers[0]

    def session_view(self) -> SessionView:
        return SessionView(session_id="session-w0", servers=self.servers)

    async def tool_names(self) -> tuple[str, ...]:
        return self.tools

    def has_recording(self, name: str) -> bool:
        return name in self.recordings

    async def shutdown(self, timeout_seconds: float) -> None:
        return None

    def answer(self, ticket_id: str, answer: str, *, via: str) -> bool:
        self.answers.append((ticket_id, answer, via))
        return True

    def subscribe_approvals(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self.approval_listeners.append(listener)
        return lambda: self.approval_listeners.remove(listener)

    def pending_approvals(self) -> tuple[Any, ...]:
        return tuple(self.tickets)


class RecordingChannel:
    """A notification channel that keeps what it was sent."""

    name = "test"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    async def send(self, item: Notification) -> None:
        if self.fail:
            raise RuntimeError("channel down: token=hunter2")
        self.sent.append(item)


def job(**fields: Any) -> dict[str, Any]:
    return {"task": "Open the page and report the price.", **fields}


def serve_settings(**fields: Any) -> ServeSettings:
    fields.setdefault("jobs", {"price-watch": job()})
    return ServeSettings(**fields)


class Rig:
    """A dispatcher over fakes, with everything a test wants to look at."""

    def __init__(self, tmp_path: Path, worker: FakeWorker | None = None, *,
                 clock: FakeClock | None = None, channel: RecordingChannel | None = None,
                 prices_known: bool = True, **settings: Any) -> None:
        self.settings = serve_settings(**settings)
        self.worker = worker or FakeWorker()
        self.clock = clock or FakeClock()
        self.sink = InMemoryEventSink()
        self.events = EventEmitter(self.sink, session_id="serve-test")
        self.ledger = Ledger(tmp_path / "ledger.jsonl", UTC)
        self.states = JobStateStore(tmp_path)
        self.channel = channel or RecordingChannel()
        self.tally = RunTally()
        self.dispatcher = Dispatcher(
            self.settings,
            self.worker,
            ledger=self.ledger,
            states=self.states,
            notifier=Notifier([self.channel], self.events, max_chars=self.settings.notify_max_chars),
            events=self.events,
            tally=self.tally,
            clock=self.clock,
        )

    def types(self) -> list[str]:
        return [record.type for record in self.sink.records]

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        return [dict(record.payload) for record in self.sink.records if record.type == event_type]

    async def drain(self) -> None:
        """Run every queued run to its end."""

        await self.dispatcher.run_pending()


def task_event(event_type: str, task_id: str, **payload: Any) -> EventRecord:
    return EventRecord(type=event_type, source="test", payload=payload, task_id=task_id)  # type: ignore[arg-type]
