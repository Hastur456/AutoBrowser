"""The inline terminal application: banner, live zone, input field, status line (§5.3-§5.6).

``TuiApp`` is a prompt_toolkit ``Application(full_screen=False)`` on the UI loop in the main
thread; the session runs on the runtime loop thread behind a :class:`ControllerPort`. Stream
items and approval tickets cross over through :meth:`RenderPump.post`; keys and commands go
back through :class:`RuntimeBridge`. The pump is the only writer to the terminal: frozen
blocks are printed above the app with ``run_in_terminal``, the live zone is redrawn by
``invalidate`` at most ``ui.refresh_hz`` times a second.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import ColorDepth, Output
from prompt_toolkit.shortcuts import print_formatted_text

from src.config import UiSettings
from src.contracts import ApprovalResolution, ApprovalTicket
from src.ui.feed import project_event
from src.ui.tui.commands import CommandResult, SlashCompleter, parse_command, run_command
from src.ui.tui.ports import ControllerPort, RuntimeBridge, RuntimePort, StreamItemView
from src.ui.tui.pump import PumpLook, RenderPump
from src.ui.tui.render import render_banner, render_live, render_rule, render_status
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.theme import Theme
from src.ui.tui.transcript import (
    ApprovalClosed,
    ApprovalShown,
    KeyAction,
    Notice,
    StreamGap,
    StreamReset,
    Transcript,
    UiMessage,
    UserSubmitted,
    approval_choices,
)
from src.ui.views import SessionView

_COLOR_DEPTHS = {
    "none": ColorDepth.DEPTH_1_BIT,
    "16": ColorDepth.DEPTH_4_BIT,
    "256": ColorDepth.DEPTH_8_BIT,
    "truecolor": ColorDepth.DEPTH_24_BIT,
}

#: ``answer`` values of the approval card choices, by the digit that picks them.
_DIGITS = ("1", "2", "3")


class TuiApp:
    """The Claude-Code-style inline terminal UI over one session."""

    def __init__(
        self,
        controller: ControllerPort,
        runtime: RuntimePort,
        *,
        settings: UiSettings,
        caps: TerminalCaps,
        initial_task: str | None = None,
        history_path: Path | None = None,
        input: Input | None = None,
        output: Output | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._controller = controller
        self._bridge = RuntimeBridge(runtime)
        self._settings = settings
        self._caps = caps
        self._initial_task = initial_task
        self._clock = clock or (lambda: datetime.now(UTC))
        self._view: SessionView | None = None
        self._task_future: Future[object] | None = None
        self._stream_future: Future[object] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._last_interrupt: float | None = None
        self._exit_code = 0
        self._theme = Theme.resolve(settings, caps)
        self._pump = RenderPump(self, PumpLook(caps, self._theme, settings), clock=self._clock)
        self._buffer = Buffer(
            multiline=True,
            history=_history(settings, history_path),
            completer=SlashCompleter(self),
            complete_while_typing=True,
        )
        self._app: Application[int] = Application(
            layout=self._layout(),
            key_bindings=self._key_bindings(),
            style=self._theme.pt_style(),
            full_screen=False,
            mouse_support=False,
            color_depth=_COLOR_DEPTHS[self._theme.color],
            min_redraw_interval=1.0 / settings.refresh_hz,
            refresh_interval=1.0 / settings.refresh_hz,
            erase_when_done=False,
            input=input,
            output=output,
        )
        self._app.ttimeoutlen = settings.escape_timeout_ms / 1000.0

    # -- CommandHost --------------------------------------------------------------------

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
        return self._pump.transcript

    @property
    def view(self) -> SessionView | None:
        return self._view

    @property
    def locale(self) -> str:
        return self._settings.locale

    @property
    def application(self) -> Application[int]:
        return self._app

    # -- Screen (the pump's port) ---------------------------------------------------------

    def print_above(self, text: str) -> None:
        output = self._app.output
        depth = _COLOR_DEPTHS[self._theme.color]

        def write() -> None:
            print_formatted_text(ANSI(text), output=output, color_depth=depth)

        if self._app.is_running:
            asyncio.ensure_future(run_in_terminal(write, in_executor=False))
        else:
            write()

    def invalidate(self) -> None:
        if self._app.is_running:
            self._app.invalidate()

    def width(self) -> int:
        return max(self._app.output.get_size().columns, 20)

    # -- Lifecycle ------------------------------------------------------------------------

    async def run_async(self) -> int:
        """Run until ``/exit``, Ctrl+D or a double Ctrl+C; returns the exit code."""

        loop = asyncio.get_running_loop()
        self._pump.bind(loop)
        self._view = await self._bridge.call(self._controller.session_view)
        if self._settings.banner:
            self._pump.print_now(
                render_banner(
                    self._view,
                    width=self.width(),
                    caps=self._caps,
                    theme=self._theme,
                    settings=self._settings,
                )
            )
        self._unsubscribe = await self._bridge.call(
            lambda: self._controller.subscribe_approvals(self._on_approval)
        )
        self._stream_future = self._bridge.spawn(self._forward_stream)
        pump_task = asyncio.ensure_future(self._pump.run())
        status_task = asyncio.ensure_future(self._poll_status())
        if self._initial_task:
            loop.call_soon(self._submit_text, self._initial_task)
        try:
            return await self._app.run_async()
        finally:
            for task in (pump_task, status_task):
                task.cancel()
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            await self._bridge.call(unsubscribe)
        if self._busy():
            await self._bridge.run(self._controller.cancel)
        if self._stream_future is not None:
            self._stream_future.cancel()

    async def _forward_stream(self) -> None:
        """Runs on the runtime loop: stream items → feed items → the pump."""

        last = 0
        async for item in self._controller.subscribe():
            message = _stream_message(item, last)
            sequence = _cursor_sequence(item)
            if sequence:
                last = sequence
            if message is not None:
                self._pump.post(message)

    def _on_approval(self, value: object) -> None:
        """Runs on the runtime loop (the broker's listener)."""

        if isinstance(value, ApprovalTicket):
            self._pump.post(ApprovalShown(value))
            if self._settings.approval_bell:
                self._bell()
        elif isinstance(value, ApprovalResolution):
            self._pump.post(ApprovalClosed(value))

    def _bell(self) -> None:
        """BEL and the window title, so a waiting approval is noticed from another window."""

        output = self._app.output
        title = (
            "AutoBrowser: нужно подтверждение" if self.locale == "ru" else "AutoBrowser: approval needed"
        )

        def ring() -> None:
            output.bell()
            if self._caps.title:
                output.set_title(title)
            output.flush()

        loop = self._app.loop
        if loop is not None:
            loop.call_soon_threadsafe(ring)

    async def _poll_status(self) -> None:
        """MCP states produce no events, so the status line polls the session view."""

        while True:
            await asyncio.sleep(self._settings.status_interval_seconds)
            try:
                self._view = await self._bridge.call(self._controller.session_view)
            except Exception:  # noqa: BLE001 - a status poll never takes the UI down
                continue
            self.invalidate()

    # -- Input ----------------------------------------------------------------------------

    def _submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if parse_command(text) is not None:
            asyncio.ensure_future(self._run_command(text))
            return
        if self._busy():
            message = (
                "задача уже выполняется — дождитесь её или нажмите Esc"
                if self.locale == "ru"
                else "a task is running — wait for it or press Esc"
            )
            self._pump.post(Notice(message, "warn"))
            return
        self._pump.post(UserSubmitted(text))
        self._task_future = self._bridge.spawn(lambda: self._controller.submit(text))
        self._task_future.add_done_callback(self._on_submitted)

    def _on_submitted(self, future: Future[object]) -> None:
        """Runs on the runtime thread; ``submit`` returns once the task is tracked."""

        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            # A failure before the goal started (the session could not start): the feed
            # has nothing about it, so say it here.
            self._pump.post(Notice(f"{type(error).__name__}: {error}", "error"))

    def _busy(self) -> bool:
        """A task runs from its submission until its outcome reached the transcript."""

        submitting = self._task_future is not None and not self._task_future.done()
        return submitting or self.transcript.task is not None

    async def _run_command(self, text: str) -> None:
        try:
            result = await run_command(self, text)
        except Exception as exc:  # noqa: BLE001 - a failing command is reported, not fatal
            self._pump.post(Notice(f"{type(exc).__name__}: {exc}", "error"))
            return
        self._apply_command(result)

    def _apply_command(self, result: CommandResult) -> None:
        for message in result.messages:
            self._pump.post(message)
        if result.theme is not None:
            settings = self._settings.model_copy(update={"theme": result.theme})
            self._theme = Theme.resolve(settings, self._caps)
            self._settings = settings
            self._app.style = self._theme.pt_style()
            self._pump.set_look(PumpLook(self._caps, self._theme, settings))
        if result.clear:
            self._app.renderer.clear()
        if result.exit:
            self._app.exit(result=self._exit_code)

    def _key(self, action: KeyAction) -> None:
        """Apply a key action at once: the next key (Enter on a card) must see its effect."""

        self._pump.apply([action])

    def _cancel(self) -> None:
        if self._busy():
            self._bridge.spawn(self._controller.cancel)

    def _answer(self, answer: str) -> None:
        card = self.transcript.block("approval")
        if card is None or card.ticket is None:
            return
        # Thread-safe: the broker resolves the waiting engine on its own loop.
        self._controller.answer(card.ticket.ticket_id, answer, via="tui")

    # -- Layout ---------------------------------------------------------------------------

    def _layout(self) -> Layout:
        live = ConditionalContainer(
            Window(
                FormattedTextControl(self._live_text),
                dont_extend_height=True,
                wrap_lines=False,
            ),
            filter=Condition(self._has_live),
        )
        input_window = Window(
            BufferControl(self._buffer),
            height=Dimension(min=1, max=10),
            wrap_lines=True,
            get_line_prefix=self._prompt_prefix,
            dont_extend_height=True,
        )
        body = HSplit(
            [
                live,
                Window(FormattedTextControl(self._rule_text), height=1),
                input_window,
                Window(FormattedTextControl(self._rule_text), height=1),
                Window(FormattedTextControl(self._status_text), height=1),
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                )
            ],
        )
        return Layout(root, focused_element=input_window)

    def _has_live(self) -> bool:
        transcript = self.transcript
        return bool(transcript.live) or transcript.task is not None

    def _live_text(self) -> StyleAndTextTuples:
        text = render_live(
            self.transcript,
            width=self.width(),
            caps=self._caps,
            theme=self._theme,
            settings=self._settings,
            now=self._clock(),
            turn_cap=self._view.turn_cap if self._view else None,
        )
        return to_formatted_text(ANSI(text))

    def _rule_text(self) -> StyleAndTextTuples:
        rule = render_rule(width=self.width(), caps=self._caps, theme=self._theme, settings=self._settings)
        return to_formatted_text(ANSI(rule))

    def _status_text(self) -> StyleAndTextTuples:
        if self._view is None:
            return []
        status = render_status(
            self._view,
            self.transcript,
            width=self.width(),
            caps=self._caps,
            theme=self._theme,
            settings=self._settings,
            now=self._clock(),
        )
        return to_formatted_text(ANSI(status))

    def _prompt_prefix(self, line: int, wrap_count: int) -> StyleAndTextTuples:
        if line == 0 and wrap_count == 0:
            return [("class:accent", f"{self._theme.glyphs.caret} ")]
        return [("", "  ")]

    # -- Keys -----------------------------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        card_open = Condition(lambda: self.transcript.block("approval") is not None)
        no_card = ~card_open

        @kb.add("enter", filter=no_card & ~has_completions)
        def _(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if buffer.text.endswith("\\"):
                buffer.delete_before_cursor(1)
                buffer.insert_text("\n")
                return
            text = buffer.text
            if text.strip():
                buffer.append_to_history()
            buffer.reset()
            self._submit_text(text)

        @kb.add("enter", filter=has_completions & no_card)
        def _(event: KeyPressEvent) -> None:
            state = event.current_buffer.complete_state
            if state is not None and state.current_completion is not None:
                event.current_buffer.apply_completion(state.current_completion)
            else:
                event.current_buffer.cancel_completion()

        @kb.add("c-j", filter=no_card)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("escape", filter=card_open, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._answer("deny")

        @kb.add("escape", filter=has_completions & no_card, eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.cancel_completion()

        @kb.add("escape", filter=~has_completions & no_card, eager=True)
        def _(event: KeyPressEvent) -> None:
            if self._busy():
                self._cancel()

        @kb.add("up", filter=card_open)
        def _(event: KeyPressEvent) -> None:
            self._key(KeyAction("approval_up"))

        @kb.add("down", filter=card_open)
        def _(event: KeyPressEvent) -> None:
            self._key(KeyAction("approval_down"))

        @kb.add("enter", filter=card_open)
        def _(event: KeyPressEvent) -> None:
            card = self.transcript.block("approval")
            if card is not None:
                self._answer(approval_choices(card)[card.choice])

        for index, digit in enumerate(_DIGITS):

            @kb.add(digit, filter=card_open)
            def _(event: KeyPressEvent, index: int = index) -> None:
                card = self.transcript.block("approval")
                if card is None:
                    return
                choices = approval_choices(card)
                if index < len(choices):
                    self._answer(choices[index])

        @kb.add("c-c")
        def _(event: KeyPressEvent) -> None:
            if self._busy():
                self._cancel()
                return
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
                return
            now = event.app.loop.time() if event.app.loop else 0.0
            if self._last_interrupt is not None and now - self._last_interrupt <= self._settings.double_press_seconds:
                event.app.exit(result=self._exit_code)
                return
            self._last_interrupt = now
            hint = "нажмите Ctrl+C ещё раз для выхода" if self.locale == "ru" else "press Ctrl+C again to exit"
            self._pump.post(Notice(hint))

        @kb.add("c-d")
        def _(event: KeyPressEvent) -> None:
            if not event.current_buffer.text and not self._busy():
                event.app.exit(result=self._exit_code)

        @kb.add("c-o")
        def _(event: KeyPressEvent) -> None:
            self._key(KeyAction("toggle_detail"))

        @kb.add("c-t")
        def _(event: KeyPressEvent) -> None:
            self._key(KeyAction("toggle_plan"))

        @kb.add("c-l")
        def _(event: KeyPressEvent) -> None:
            event.app.renderer.clear()

        return kb


def _stream_message(item: StreamItemView, last_sequence: int) -> UiMessage | None:
    if item.kind == "event" and item.record is not None:
        return project_event(item.record)
    if item.kind == "gap":
        missed = max(_cursor_sequence(item) - last_sequence, 0)
        return StreamGap(missed)
    if item.kind == "reset":
        return StreamReset()
    return None


def _cursor_sequence(item: StreamItemView) -> int:
    sequence = item.cursor.sequence if item.cursor is not None else 0
    return sequence if isinstance(sequence, int) else 0


def _history(settings: UiSettings, path: Path | None) -> History:
    if not settings.input_history or path is None:
        return InMemoryHistory()
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(path))



__all__ = ["TuiApp"]
