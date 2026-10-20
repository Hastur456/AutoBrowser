"""The plain front end: one line per feed item and ``input()`` prompts, no prompt_toolkit.

Used for terminals the TUI cannot drive (``TERM=dumb``, mintty without ConPTY, a pipe) and
for ``ui.frontend=plain``. Everything goes through one writer function under a lock, so the
stream forwarder on the runtime thread and the prompts on the main thread never interleave
within a line. No ANSI is written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from src.config import UiSettings
from src.contracts import ApprovalResolution, ApprovalTicket
from src.ui.feed import project_event, sanitize_text
from src.ui.tui.commands import CommandResult, parse_command, run_command
from src.ui.tui.ports import ControllerPort, RuntimeBridge, RuntimePort
from src.ui.tui.strings import Strings
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.transcript import CommandOutput, Notice, Transcript
from src.ui.views import FeedItem, SessionView

_LOG = logging.getLogger("autobrowser.ui")

PROMPT = "autobrowser> "
#: Width of the ``[tag]`` column, so the text after it lines up.
_TAG_WIDTH = 7


@dataclass
class PlainState:
    """What the line formatter remembers between items of one task."""

    tool_started: dict[str, datetime] = field(default_factory=dict)
    goal_started: datetime | None = None
    calls: int = 0


def format_item(item: FeedItem, state: PlainState, settings: UiSettings) -> list[str]:
    """The lines of one feed item (often none); pure apart from ``state``."""

    if item.kind == "goal_started":
        state.goal_started = item.timestamp
        state.calls = 0
        return []
    if item.kind == "plan":
        steps = " ".join(f"{index + 1}) {step}" for index, step in enumerate(item.steps))
        return [_tag("plan", steps)] if steps else []
    if item.kind == "call":
        return [_tag("call", " ".join(part for part in (item.tool, _args(item.args, settings)) if part))]
    if item.kind == "tool_started":
        if item.timestamp is not None:
            state.tool_started[item.tool] = item.timestamp
        return []
    if item.kind == "tool_finished":
        state.calls += 1
        started = state.tool_started.pop(item.tool, None)
        duration = ""
        if started is not None and item.timestamp is not None:
            duration = f"{max((item.timestamp - started).total_seconds(), 0.0):.1f}s"
        ok = item.status == "success"
        detail = item.url if ok and item.url else _first_line(item.text)
        return [_tag("ok" if ok else "fail", " ".join(p for p in (duration, _cut(detail, settings)) if p))]
    if item.kind == "gate":
        if item.decision == "allow":
            return []
        tag = "ask" if item.decision == "ask" else "block"
        head = " ".join(p for p in (item.origin, item.rule) if p)
        return [_tag(tag, f"{head}: {item.text}" if item.text else f"{head} {item.decision}")]
    if item.kind == "outcome":
        facts = []
        if item.turns is not None:
            facts.append(f"{item.turns} turns")
        if state.goal_started is not None and item.timestamp is not None:
            seconds = max(int((item.timestamp - state.goal_started).total_seconds()), 0)
            facts.append(f"{seconds // 60}:{seconds % 60:02d}")
        tag = {"completed": "done"}.get(item.status, item.status)
        lines = [_tag(tag, ", ".join(facts))]
        if item.text:
            lines.extend(item.text.split("\n"))
        return lines
    if item.kind == "memory":
        return [_tag("memory", f"{item.status}: {item.text}".rstrip(": "))]
    return []


def format_ticket(ticket: ApprovalTicket) -> list[str]:
    tool, server = sanitize_text(ticket.tool), sanitize_text(ticket.server)
    domain, rule_id = sanitize_text(ticket.domain), sanitize_text(ticket.rule_id)
    target = f"{server}/{tool}" if server else tool
    where = f" on {domain}" if domain else ""
    rule = f" (rule {rule_id})" if rule_id else ""
    lines = [_tag("ask", f"{target}{where}{rule}")]
    if ticket.reason:
        lines.append(" " * (_TAG_WIDTH + 1) + sanitize_text(ticket.reason))
    if ticket.args_preview:
        lines.append(" " * (_TAG_WIDTH + 1) + sanitize_text(ticket.args_preview))
    return lines


def ticket_prompt(ticket: ApprovalTicket) -> str:
    options = "1) once  2) session  3) deny" if ticket.allow_session else "1) once  3) deny"
    return " " * (_TAG_WIDTH + 1) + options + " > "


def parse_answer(text: str, ticket: ApprovalTicket) -> str:
    """``once``/``session``/``deny`` from what was typed; anything unclear is a deny."""

    value = text.strip().lower()
    if value in {"1", "once", "y", "yes", "да", "д"}:
        return "once"
    if value in {"2", "session", "s"} and ticket.allow_session:
        return "session"
    return "deny"


class PlainFrontend:
    """A line-based session loop; also a :class:`CommandHost` for the shared slash commands."""

    def __init__(
        self,
        controller: ControllerPort,
        runtime: RuntimePort,
        *,
        settings: UiSettings,
        caps: TerminalCaps,
        initial_task: str | None = None,
        write: Callable[[str], None] | None = None,
        read: Callable[[str], str] | None = None,
    ) -> None:
        self._controller = controller
        self._bridge = RuntimeBridge(runtime)
        self._runtime = runtime
        self._settings = settings
        self._caps = caps
        self._initial_task = initial_task
        self._write = write or _print
        self._read = read or input
        self._lock = threading.Lock()
        self._events: queue.Queue[FeedItem | ApprovalTicket | ApprovalResolution] = queue.Queue()
        self._state = PlainState()
        self._view: SessionView | None = None
        self._transcript = Transcript()

    # -- CommandHost ------------------------------------------------------------------------

    @property
    def controller(self) -> ControllerPort:
        return self._controller

    @property
    def bridge(self) -> RuntimeBridge:
        return self._bridge

    @property
    def caps(self) -> TerminalCaps:
        return self._caps

    @property
    def transcript(self) -> Transcript:
        return self._transcript

    @property
    def view(self) -> SessionView | None:
        return self._view

    @property
    def locale(self) -> str:
        return self._settings.locale

    def notice(self, text: str) -> None:
        """Show a session message as one line; safe from any thread."""

        self._emit(text)

    # -- Loop -------------------------------------------------------------------------------

    def run(self) -> int:
        """Prompt, run each task to its outcome, repeat; returns the exit code."""

        self._view = self._runtime.submit(_call(self._controller.session_view)).result()
        view = self._view
        self._emit(
            f"AutoBrowser {view.version} · {view.model or '?'} · session {view.session_id[:8]}".replace("  ", " ")
        )
        if view.resumed:
            self._emit(
                Strings(self._settings.locale)(
                    "resumed", session=view.session_id[:8], count=view.task_count
                )
            )
        unsubscribe = self._runtime.submit(
            _call(lambda: self._controller.subscribe_approvals(self._events.put))
        ).result()
        stream = self._runtime.submit(self._forward_stream())
        exit_code = 0
        try:
            pending = self._initial_task
            while True:
                if pending is None:
                    try:
                        pending = self._read(PROMPT)
                    except (EOFError, KeyboardInterrupt):
                        break
                text, pending = pending.strip(), None
                if not text:
                    continue
                if parse_command(text) is not None:
                    result = self._runtime_command(text)
                    if result.exit:
                        break
                    continue
                if not self._run_task(text):
                    exit_code = 1
        finally:
            stream.cancel()
            self._runtime.submit(_call(unsubscribe)).result()
        return exit_code

    def _run_task(self, text: str) -> bool:
        """Submit ``text`` and print its feed until the outcome; ``True`` when it completed."""

        # Leftovers of an earlier task (an interrupted wait) must not end this one.
        self._drain()
        # Short timeouts keep Ctrl+C responsive: blocking waits are not interruptible on
        # Windows.
        tick = 1.0 / self._settings.refresh_hz
        submitted = self._runtime.submit(self._controller.submit(text))
        try:
            while True:
                try:
                    task = submitted.result(timeout=tick)
                    break
                except TimeoutError:
                    continue
        except KeyboardInterrupt:
            # Ctrl+C while the session starts (Chrome, MCP): cancel the start and wait for
            # its cleanup instead of leaving it running under a closing session.
            self._runtime.submit(self._controller.cancel()).result()
            self._emit(_tag("cancelled", ""))
            return False
        except BaseException as exc:  # noqa: BLE001 - shown to the user, the loop goes on
            self._emit(_tag("failed", f"{type(exc).__name__}: {exc}"))
            return False
        checks_every = max(int(self._settings.status_interval_seconds * self._settings.refresh_hz), 1)
        idle_ticks = 0
        idle_checks = 0
        while True:
            try:
                value = self._events.get(timeout=tick)
            except queue.Empty:
                idle_ticks += 1
                if idle_ticks % checks_every:
                    continue
                if not self._settled():
                    idle_checks = 0
                    continue
                # Twice in a row, so an outcome still on its way is not raced.
                idle_checks += 1
                if idle_checks < 2:
                    continue
                return self._lost_outcome(task.task_id)
            except KeyboardInterrupt:
                self._runtime.submit(self._controller.cancel()).result()
                continue
            idle_ticks = idle_checks = 0
            if _task_of(value) not in {task.task_id, ""}:
                continue
            if isinstance(value, ApprovalTicket):
                self._ask(value)
                continue
            if isinstance(value, ApprovalResolution):
                continue
            for line in format_item(value, self._state, self._settings):
                self._emit(line)
            if value.kind == "outcome":
                return value.status == "completed"

    def _lost_outcome(self, task_id: str) -> bool:
        """The task ended but its outcome never reached the feed: report the recorded one."""

        views = self._runtime.submit(_call(self._controller.task_views)).result()
        status = next((view.status for view in views if view.task_id == task_id), "")
        lost = (
            "задача завершилась, но итог не дошёл до ленты"
            if self.locale == "ru"
            else "the task ended but its outcome never reached the feed"
        )
        self._emit(_tag("warn", f"{lost} ({status})" if status else lost))
        return status == "completed"

    def _drain(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _ask(self, ticket: ApprovalTicket) -> None:
        for line in format_ticket(ticket):
            self._emit(line)
        try:
            typed = self._read(ticket_prompt(ticket))
        except (EOFError, KeyboardInterrupt):
            typed = ""
        self._controller.answer(ticket.ticket_id, parse_answer(typed, ticket), via="plain")

    def _runtime_command(self, text: str) -> CommandResult:
        async def run() -> CommandResult:
            return await run_command(self, text)

        try:
            # Commands await the bridge, which needs a running loop on this thread.
            result = asyncio.run(run())
        except Exception as exc:  # noqa: BLE001 - a failing command is reported, not fatal
            self._emit(_tag("error", f"{type(exc).__name__}: {exc}"))
            return CommandResult()
        for message in result.messages:
            if isinstance(message, CommandOutput):
                self._emit(message.title)
                for line in message.lines:
                    self._emit(f"  {line}")
            elif isinstance(message, Notice):
                self._emit(message.text)
        return result

    def _settled(self) -> bool:
        """No task runs and nothing is left in the queue to show."""

        view = self._runtime.submit(_call(self._controller.session_view)).result()
        return not view.busy and self._events.empty()

    async def _forward_stream(self) -> None:
        try:
            async for item in self._controller.subscribe():
                if item.kind != "event" or item.record is None:
                    continue
                feed = project_event(item.record)
                if feed is not None:
                    self._events.put(feed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop notices the silence and moves on
            _LOG.exception("event stream forwarder failed")
            self._emit(_tag("error", f"{type(exc).__name__}: {exc}"))

    def _emit(self, line: str) -> None:
        with self._lock:
            self._write(line)


def _task_of(value: FeedItem | ApprovalTicket | ApprovalResolution) -> str:
    """The task an item belongs to; ``""`` when it does not say (resolutions)."""

    if isinstance(value, (FeedItem, ApprovalTicket)):
        return value.task_id
    return ""


def _tag(tag: str, text: str) -> str:
    return f"{'[' + tag + ']':<{_TAG_WIDTH}} {text}".rstrip()


def _args(args: Mapping[str, object], settings: UiSettings) -> str:
    parts = []
    for key, value in args.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        parts.append(f"{key}={shown}")
    return _cut(" ".join(parts), settings)


def _cut(text: str, settings: UiSettings) -> str:
    text = text.replace("\n", " ")
    limit = settings.preview_chars
    return text if len(text) <= limit else text[:limit] + "..."


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.split("\n") if line.strip()), "")


async def _call(fn: Callable[[], object]) -> object:
    return fn()


def _print(line: str) -> None:
    print(line, flush=True)


__all__ = [
    "PROMPT",
    "PlainFrontend",
    "PlainState",
    "format_item",
    "format_ticket",
    "parse_answer",
    "ticket_prompt",
]
