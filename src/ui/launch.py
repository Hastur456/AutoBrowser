"""Pick and run a front end for an interactive session (§6.2 rule 8, §6.3).

``ui.frontend`` and the detected :class:`TerminalCaps` choose the TUI or the plain front end;
a TUI that cannot start on this console (prompt_toolkit's ``NoConsoleScreenBufferError`` and
the like) falls back to plain with the reason, instead of breaking the terminal. Both run
inside :func:`terminal_quiet`, so the front end is the only writer to the terminal.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from src.config import UiSettings
from src.ui.logging_setup import terminal_quiet
from src.ui.plain import PlainFrontend
from src.ui.tui.ports import ControllerPort, RuntimePort
from src.ui.tui.terminal import TerminalCaps, TerminalStreams, detect_terminal, process_env

_LOG = logging.getLogger("autobrowser.ui")


class OutputRelay:
    """The session's ``output_fn``: forwards messages to whichever front end is attached.

    The session is built before the front end exists; until one attaches (and after it
    detaches) messages go to the log, never to the terminal.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: Callable[[str], None] | None = None

    def attach(self, target: Callable[[str], None]) -> Callable[[], None]:
        with self._lock:
            self._target = target

        def detach() -> None:
            with self._lock:
                if self._target is target:
                    self._target = None

        return detach

    def __call__(self, *values: object, **_print_kwargs: object) -> None:
        text = " ".join(str(value) for value in values)
        with self._lock:
            target = self._target
        if target is None:
            _LOG.info("%s", text)
        else:
            target(text)


def detect(
    settings: UiSettings,
    env: Mapping[str, str] | None = None,
    streams: TerminalStreams | None = None,
) -> TerminalCaps:
    return detect_terminal(settings, env if env is not None else process_env(), streams or TerminalStreams.from_process())


def run_frontend(
    controller: ControllerPort,
    runtime: RuntimePort,
    *,
    settings: UiSettings,
    log_file: Path,
    initial_task: str | None = None,
    history_path: Path | None = None,
    caps: TerminalCaps | None = None,
    output: OutputRelay | None = None,
) -> int:
    """Run the interactive front end until the user leaves; returns the exit code.

    ``output`` is the session's ``output_fn``; its messages become front-end notices.
    """

    caps = caps or detect(settings)
    relay = output or OutputRelay()
    with terminal_quiet(log_file):
        if caps.level != "plain":
            try:
                from src.ui.tui.app import TuiApp

                app = TuiApp(
                    controller,
                    runtime,
                    settings=settings,
                    caps=caps,
                    initial_task=initial_task,
                    history_path=history_path,
                )
            except Exception as exc:  # noqa: BLE001 - any console init failure degrades to plain
                _LOG.warning("terminal UI unavailable, using plain mode", exc_info=exc)
                print(f"[plain] terminal UI unavailable ({type(exc).__name__}: {exc}); see {log_file}")
            else:
                detach = relay.attach(app.notice)
                try:
                    return asyncio.run(app.run_async())
                finally:
                    detach()
        plain = PlainFrontend(
            controller, runtime, settings=settings, caps=caps, initial_task=initial_task
        )
        detach = relay.attach(plain.notice)
        try:
            return plain.run()
        finally:
            detach()


__all__ = ["OutputRelay", "detect", "run_frontend"]
