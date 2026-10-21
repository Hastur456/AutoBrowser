"""The render pump: the only writer to the terminal while the TUI runs (§6.2).

Every update — stream items, approval tickets, notices, key actions — becomes a
:data:`UiMessage` posted to the pump from any thread (:meth:`RenderPump.post`). The pump
drains its queue at most ``ui.refresh_hz`` times a second on the UI loop, folds the messages
through the pure :func:`reduce`, prints newly frozen blocks into the scrollback exactly once
and asks the screen to redraw the live zone.

The pump never touches prompt_toolkit directly: it talks to a :class:`Screen` port, so the
throttling and freezing logic is tested without a terminal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.config import UiSettings
from src.ui.tui.render import render_block
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.theme import Theme
from src.ui.tui.transcript import Block, Transcript, UiMessage, reduce


class Screen(Protocol):
    """Where the pump writes: frozen text above the app, and a live-zone redraw."""

    def print_above(self, text: str) -> None: ...

    def invalidate(self) -> None: ...

    def width(self) -> int: ...


@dataclass(frozen=True)
class PumpLook:
    """How blocks are drawn; replaced as a whole by ``/theme`` or a resize."""

    caps: TerminalCaps
    theme: Theme
    settings: UiSettings


class RenderPump:
    """Queue → throttle → freeze into the scrollback → redraw the live zone."""

    def __init__(
        self,
        screen: Screen,
        look: PumpLook,
        *,
        clock: Callable[[], datetime] | None = None,
        on_frozen: Callable[[Block], None] | None = None,
    ) -> None:
        self._screen = screen
        self._look = look
        self._clock = clock or (lambda: datetime.now(UTC))
        self._on_frozen = on_frozen
        self._transcript = Transcript(detail=look.settings.detail)
        self._queue: asyncio.Queue[UiMessage] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._printed_any = False

    @property
    def transcript(self) -> Transcript:
        return self._transcript

    @property
    def look(self) -> PumpLook:
        return self._look

    def set_look(self, look: PumpLook) -> None:
        """New theme or caps for future frozen blocks and the live zone (frozen output stays)."""

        self._look = look
        self._screen.invalidate()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the UI loop; call once on that loop before posting from other threads."""

        self._loop = loop
        self._queue = asyncio.Queue()

    def post(self, message: UiMessage) -> None:
        """Hand a message to the pump; safe from any thread once bound."""

        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            raise RuntimeError("RenderPump.post() before bind().")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            queue.put_nowait(message)
        elif not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, message)

    async def run(self) -> None:
        """Drain the queue frame by frame until cancelled."""

        queue = self._queue
        if queue is None:
            raise RuntimeError("RenderPump.run() before bind().")
        interval = 1.0 / self._look.settings.refresh_hz
        while True:
            message = await queue.get()
            batch = [message]
            # Coalesce everything that arrived within one frame.
            await asyncio.sleep(interval)
            while not queue.empty():
                batch.append(queue.get_nowait())
            self.apply(batch)

    def apply(self, messages: list[UiMessage]) -> None:
        """Fold ``messages`` in order, print what froze, then redraw once."""

        frozen: list[Block] = []
        transcript = self._transcript
        for message in messages:
            transcript, newly = reduce(transcript, message)
            frozen.extend(newly)
        self._transcript = transcript
        if frozen:
            self._screen.print_above(self._render_frozen(frozen))
            if self._on_frozen is not None:
                for block in frozen:
                    self._on_frozen(block)
        self._screen.invalidate()

    def print_now(self, text: str) -> None:
        """Print preformatted text (the banner) above the app, keeping the block rhythm."""

        self._screen.print_above(self._spaced(text))

    def _render_frozen(self, blocks: list[Block]) -> str:
        look = self._look
        width = self._screen.width()
        rendered = [
            render_block(
                block,
                width=width,
                caps=look.caps,
                theme=look.theme,
                settings=look.settings,
                now=self._clock(),
            )
            for block in blocks
        ]
        separator = "\n\n" if look.settings.density == "comfortable" else "\n"
        return self._spaced(separator.join(text for text in rendered if text))

    def _spaced(self, text: str) -> str:
        """One blank line between blocks in the comfortable density, none before the first."""

        if self._printed_any and self._look.settings.density == "comfortable":
            text = "\n" + text
        self._printed_any = True
        return text


__all__ = ["PumpLook", "RenderPump", "Screen"]
