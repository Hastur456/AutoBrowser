from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.config import UiSettings
from src.contracts import ApprovalTicket
from src.ui.plain import PlainFrontend, PlainState, format_item, format_ticket, parse_answer
from src.ui.runtime_loop import RuntimeLoop
from src.ui.tui.terminal import TerminalCaps
from src.ui.views import FeedItem, SessionView, TaskView

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SETTINGS = UiSettings()


def item(kind: str, at: float = 0.0, **fields: object) -> FeedItem:
    return FeedItem(kind=kind, event_type=kind, timestamp=T0 + timedelta(seconds=at), **fields)  # type: ignore[arg-type]


def ticket(allow_session: bool = True) -> ApprovalTicket:
    return ApprovalTicket(
        ticket_id="k1",
        task_id="t1",
        tool="browser_click",
        server="playwright",
        domain="www.ozon.ru",
        reason="checkout",
        rule_id="ask-checkout",
        source="rule",
        args_preview='{"ref": "e1"}',
        allow_session=allow_session,
        created_at=T0,
        deadline=T0 + timedelta(seconds=60),
    )


def test_format_items_like_the_plain_mockup() -> None:
    state = PlainState()
    lines: list[str] = []
    for feed in [
        item("goal_started"),
        item("plan", steps=("Открыть ozon.ru", "Найти", "Выбрать")),
        item("call", tool="browser_navigate", args={"url": "https://www.ozon.ru"}),
        item("gate", decision="allow", origin="permission"),
        item("tool_started", 0.0, tool="browser_navigate"),
        item("tool_finished", 1.2, tool="browser_navigate", status="success", url="https://www.ozon.ru/"),
        item("gate", decision="deny", origin="hook", rule="url_policy", text="домен вне списка"),
        item("tool_finished", 2.0, tool="x", status="error", text="boom\nmore"),
        item("outcome", 72.0, status="completed", turns=6, text="Нашёл 3 варианта"),
    ]:
        lines.extend(format_item(feed, state, SETTINGS))

    assert lines == [
        "[plan]  1) Открыть ozon.ru 2) Найти 3) Выбрать",
        "[call]  browser_navigate url=https://www.ozon.ru",
        "[ok]    1.2s https://www.ozon.ru/",
        "[block] hook url_policy: домен вне списка",
        "[fail]  boom",
        "[done]  6 turns, 1:12",
        "Нашёл 3 варианта",
    ]


def test_ticket_lines_and_answers() -> None:
    assert format_ticket(ticket())[0] == "[ask]   playwright/browser_click on www.ozon.ru (rule ask-checkout)"
    assert parse_answer("1", ticket()) == "once"
    assert parse_answer("2", ticket()) == "session"
    assert parse_answer("2", ticket(allow_session=False)) == "deny"
    assert parse_answer("", ticket()) == "deny"
    assert parse_answer("whatever", ticket()) == "deny"


@dataclass(frozen=True)
class Cursor:
    session_id: str
    sequence: int


@dataclass(frozen=True)
class Item:
    kind: str
    cursor: Cursor
    record: dict[str, object] | None = None


class ScriptedController:
    """Plays one task's events after ``submit``, asking for one approval in between."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Item] | None = None
        self.listeners: list[Callable[[object], None]] = []
        self.answers: list[tuple[str, str, str]] = []
        self.submitted: list[str] = []

    def session_view(self) -> SessionView:
        return SessionView(session_id="abcdef123456", version="0.1.0", model="fake")

    async def submit(self, text: str) -> TaskView:
        self.submitted.append(text)
        asyncio.get_running_loop().create_task(self._play())
        return TaskView(index=1, task_id="t1", task=text, status="running")

    async def _play(self) -> None:
        while self.queue is None:
            await asyncio.sleep(0)
        self._event(1, "goal.started", {"task": "t"}, 0)
        self._event(2, "action.proposed", {"tool_request": {"name": "click", "args": {"ref": "e1"}}}, 1)
        for listener in self.listeners:
            listener(ticket())
        while not self.answers:
            await asyncio.sleep(0.01)
        self._event(3, "goal.completed", {"status": "completed", "final_answer": "ok", "turns": 2}, 5)

    def _event(self, sequence: int, event_type: str, payload: dict[str, object], at: float) -> None:
        assert self.queue is not None
        record = {"type": event_type, "payload": payload, "task_id": "t1", "sequence": sequence,
                  "timestamp": (T0 + timedelta(seconds=at)).isoformat()}
        self.queue.put_nowait(Item("event", Cursor("s", sequence), record))

    async def cancel(self) -> bool:
        return False

    def answer(self, ticket_id: str, answer: str, *, via: str) -> bool:
        self.answers.append((ticket_id, answer, via))
        return True

    def task_views(self) -> list[TaskView]:
        return []

    async def _items(self) -> AsyncIterator[Item]:
        self.queue = asyncio.Queue()
        while True:
            yield await self.queue.get()

    def subscribe(self, after: object = None) -> AsyncIterator[Item]:
        return self._items()

    def subscribe_approvals(self, listener: Callable[[object], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: None


@pytest.fixture
def runtime() -> Iterator[RuntimeLoop]:
    loop = RuntimeLoop()
    yield loop
    loop.stop()


def plain_caps() -> TerminalCaps:
    return TerminalCaps(
        level="plain", color="none", unicode=False, sync_output=False, hyperlinks=False,
        title=False, width=80, height=24, reason="stdin is not a terminal",
    )


def test_plain_loop_runs_a_task_answers_an_approval_and_exits(runtime: RuntimeLoop) -> None:
    controller = ScriptedController()
    written: list[str] = []
    replies = iter(["2", "/exit"])
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    frontend = PlainFrontend(
        controller, runtime, settings=SETTINGS, caps=plain_caps(), initial_task="купи", write=written.append, read=read
    )

    assert frontend.run() == 0
    assert controller.submitted == ["купи"]
    assert controller.answers == [("k1", "session", "plain")]
    assert written[0] == "AutoBrowser 0.1.0 · fake · session abcdef12"
    assert "[call]  click ref=e1" in written
    assert "[done]  2 turns, 0:05" in written
    assert written[-1] == "ok"
    assert "\x1b" not in "".join(written)
    assert prompts[0].endswith("1) once  2) session  3) deny > ")


def test_plain_loop_ends_on_eof(runtime: RuntimeLoop) -> None:
    def read(_prompt: str) -> str:
        raise EOFError

    frontend = PlainFrontend(
        ScriptedController(), runtime, settings=SETTINGS, caps=plain_caps(), write=lambda _l: None, read=read
    )

    assert frontend.run() == 0
