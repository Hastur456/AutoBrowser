"""Keys and commands of ``TuiApp`` through prompt_toolkit pipe input and a fake controller.

The controller runs on a real :class:`RuntimeLoop` thread, as in production, so every call
crosses the UI ↔ runtime boundary the same way.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from src.config import UiSettings
from src.contracts import ApprovalResolution, ApprovalTicket
from src.ui.runtime_loop import RuntimeLoop
from src.ui.tui.app import TuiApp
from src.ui.tui.terminal import TerminalCaps
from src.ui.views import ServerView, SessionView, TaskView

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SETTINGS = UiSettings(refresh_hz=30.0, status_interval_seconds=60.0, escape_timeout_ms=0)


@dataclass(frozen=True)
class Cursor:
    session_id: str
    sequence: int


@dataclass(frozen=True)
class Item:
    kind: str
    cursor: Cursor
    record: dict[str, object] | None = None


class FakeController:
    """Records commands; events and tickets are pushed by the test."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled = 0
        self.answers: list[tuple[str, str, str]] = []
        self.listeners: list[Callable[[object], None]] = []
        self.queue: asyncio.Queue[Item] | None = None
        self.sequence = 0
        self.running = False

    async def start(self) -> SessionView:
        return self.session_view()

    async def submit(self, text: str) -> TaskView:
        self.submitted.append(text)
        self.running = True
        return TaskView(index=len(self.submitted), task_id="t1", task=text, status="running")

    async def cancel(self) -> bool:
        self.cancelled += 1
        return self.running

    async def reconnect(self, server: str) -> ServerView:
        return ServerView(server, "ready")

    async def reset(self) -> SessionView:
        return self.session_view()

    def answer(self, ticket_id: str, answer: str, *, via: str) -> bool:
        self.answers.append((ticket_id, answer, via))
        return True

    def session_view(self) -> SessionView:
        return SessionView(
            session_id="9c441f7e0000",
            version="0.1.0",
            model="fake-model",
            servers=(ServerView("playwright", "ready", tool_count=3),),
            permission_mode="default",
            turn_cap=50,
        )

    def task_views(self) -> list[TaskView]:
        return [TaskView(index=i + 1, task_id=f"t{i}", task=t, status="completed") for i, t in enumerate(self.submitted)]

    def history(self, limit: int) -> list[dict[str, object]]:
        return [{"role": "user", "content": "hi"}][:limit]

    def memory_entries(self) -> list[object]:
        return []

    def current_url(self) -> str:
        return "https://shop.test/cart"

    def replay(self, target: str = "") -> list[str]:
        if target == "9":
            raise LookupError("Task numbers run from 1 to 1.")
        return ["#1 · completed", "goal.started: go", "1. any_tool {}"]

    async def _items(self) -> AsyncIterator[Item]:
        self.queue = asyncio.Queue()
        while True:
            yield await self.queue.get()

    def subscribe(self, after: object = None) -> AsyncIterator[Item]:
        return self._items()

    def subscribe_approvals(self, listener: Callable[[object], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    # test helpers, called on the runtime loop
    def push(self, event_type: str, payload: dict[str, object], at: float = 0.0) -> None:
        self.sequence += 1
        record = {
            "type": event_type,
            "payload": payload,
            "task_id": "t1",
            "sequence": self.sequence,
            "timestamp": (T0 + timedelta(seconds=at)).isoformat(),
        }
        assert self.queue is not None
        self.queue.put_nowait(Item("event", Cursor("s", self.sequence), record))


def caps() -> TerminalCaps:
    return TerminalCaps(
        level="full",
        color="16",
        unicode=True,
        sync_output=False,
        hyperlinks=False,
        title=False,
        width=80,
        height=24,
        reason="test",
    )


@pytest.fixture
def runtime() -> Iterator[RuntimeLoop]:
    loop = RuntimeLoop()
    yield loop
    loop.stop()


class Harness:
    def __init__(self, app: TuiApp, controller: FakeController, runtime: RuntimeLoop, out: io.StringIO, send: Callable[[str], None]) -> None:
        self.app, self.controller, self.runtime, self.out, self.send = app, controller, runtime, out, send

    def on_runtime(self, fn: Callable[[], object]) -> None:
        async def call() -> object:
            return fn()

        self.runtime.submit(call()).result(timeout=5)


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def start(runtime: RuntimeLoop, settings: UiSettings = SETTINGS) -> tuple[Harness, asyncio.Task[int], object]:
    controller = FakeController()
    pipe_context = create_pipe_input()
    pipe = pipe_context.__enter__()
    out = io.StringIO()
    output = Vt100_Output(out, lambda: Size(rows=24, columns=80), term="xterm")
    app = TuiApp(controller, runtime, settings=settings, caps=caps(), input=pipe, output=output, clock=lambda: T0)
    task = asyncio.create_task(app.run_async())
    harness = Harness(app, controller, runtime, out, pipe.send_text)
    await until(lambda: app.application.is_running and controller.queue is not None and bool(controller.listeners))
    return harness, task, pipe_context


async def stop(harness: Harness, task: asyncio.Task[int], pipe: object) -> int:
    harness.send("\x04")  # Ctrl+D on an empty line
    code = await asyncio.wait_for(task, 5)
    pipe.__exit__(None, None, None)  # type: ignore[attr-defined]
    return code


@pytest.mark.asyncio
async def test_banner_then_enter_submits_and_busy_input_is_refused(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)

    assert "AutoBrowser 0.1.0" in h.out.getvalue()
    h.send("найди наушники\r")
    await until(lambda: h.controller.submitted == ["найди наушники"])
    h.on_runtime(lambda: h.controller.push("goal.started", {"task": "найди наушники"}))
    await until(lambda: h.app.transcript.task is not None)

    h.send("ещё одна\r")
    await asyncio.sleep(0.2)
    assert h.controller.submitted == ["найди наушники"]
    assert "задача уже выполняется" in h.out.getvalue()

    h.on_runtime(lambda: h.controller.push("goal.completed", {"status": "completed", "final_answer": "Готово!", "turns": 2}, 5))
    await until(lambda: h.app.transcript.task is None)
    await until(lambda: "Готово!" in h.out.getvalue())
    assert await stop(h, task, pipe) == 0


@pytest.mark.asyncio
async def test_escape_cancels_a_running_task(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)
    h.send("go\r")
    await until(lambda: h.controller.submitted == ["go"])
    h.on_runtime(lambda: h.controller.push("goal.started", {"task": "go"}))
    await until(lambda: h.app.transcript.task is not None)

    h.send("\x1b")
    await until(lambda: h.controller.cancelled == 1)
    h.on_runtime(lambda: h.controller.push("goal.cancelled", {"status": "cancelled", "reason": "stop"}))
    await until(lambda: h.app.transcript.task is None)
    assert await stop(h, task, pipe) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("keys, answer", [("\r", "once"), ("\x1b[B\r", "session"), ("3", "deny"), ("\x1b", "deny")])
async def test_approval_card_keys(runtime: RuntimeLoop, keys: str, answer: str) -> None:
    h, task, pipe = await start(runtime)
    ticket = ApprovalTicket(
        ticket_id="k1",
        task_id="t1",
        tool="browser_click",
        server="playwright",
        domain="shop.test",
        reason="checkout",
        rule_id="ask",
        source="rule",
        args_preview="{}",
        allow_session=True,
        created_at=T0,
        deadline=T0 + timedelta(seconds=60),
    )
    h.on_runtime(lambda: [listener(ticket) for listener in h.controller.listeners])
    await until(lambda: h.app.transcript.block("approval") is not None)

    h.send(keys)
    await until(lambda: bool(h.controller.answers))
    assert h.controller.answers == [("k1", answer, "tui")]

    resolution = ApprovalResolution(ticket_id="k1", answer=answer, via="tui", resolved_at=T0)  # type: ignore[arg-type]
    h.on_runtime(lambda: [listener(resolution) for listener in h.controller.listeners])
    await until(lambda: h.app.transcript.block("approval") is None)
    assert await stop(h, task, pipe) == 0


@pytest.mark.asyncio
async def test_double_ctrl_c_exits_and_single_one_hints(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)

    h.send("\x03")
    await until(lambda: "Ctrl+C ещё раз" in h.out.getvalue())
    assert not task.done()
    h.send("\x03")
    assert await asyncio.wait_for(task, 5) == 0
    pipe.__exit__(None, None, None)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_slash_commands_run_and_exit(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)

    h.send("/tasks\r")
    await until(lambda: "Задачи сессии" in h.out.getvalue())
    h.send("/detail verbose\r")
    await until(lambda: h.app.transcript.detail == "verbose")
    h.send("/nope\r")
    await until(lambda: "неизвестная команда: /nope" in h.out.getvalue())
    h.send("/exit\r")
    assert await asyncio.wait_for(task, 5) == 0
    pipe.__exit__(None, None, None)  # type: ignore[attr-defined]
    assert h.controller.submitted == []


@pytest.mark.asyncio
async def test_ctrl_o_and_ctrl_t_toggle_views(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)

    h.send("\x0f")  # Ctrl+O
    await until(lambda: h.app.transcript.detail == "verbose")
    h.send("\x14")  # Ctrl+T
    await until(lambda: h.app.transcript.show_plan is False)
    assert await stop(h, task, pipe) == 0


@pytest.mark.asyncio
async def test_url_and_replay_commands(runtime: RuntimeLoop) -> None:
    h, task, pipe = await start(runtime)

    h.send("/url\r")
    await until(lambda: "https://shop.test/cart" in h.out.getvalue())
    h.send("/replay\r")
    await until(lambda: "1. any_tool {}" in h.out.getvalue())
    h.send("/replay 9\r")
    await until(lambda: "Task numbers run from 1 to 1." in h.out.getvalue())
    assert await stop(h, task, pipe) == 0
