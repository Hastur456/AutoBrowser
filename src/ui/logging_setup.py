"""Keep log records, warnings and thread tracebacks off the terminal while a UI owns it (B2).

Inside :func:`terminal_quiet` the root logger writes only to ``ui.log_file``, ``warnings`` are
captured into logging, and uncaught exceptions of the main thread and of other threads are
logged instead of printed. Everything is restored on exit, so a crash still leaves a usable
terminal and a traceback in the log. Child process stderr (MCP servers, Chrome) is routed by
the session itself.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

_LOG = logging.getLogger("autobrowser.ui")
_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"


@contextmanager
def terminal_quiet(log_file: Path) -> Iterator[Path]:
    """Route logging, warnings and uncaught exceptions to ``log_file`` for the block."""

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_excepthook = sys.excepthook
    saved_thread_hook = threading.excepthook

    def log_exception(
        kind: type[BaseException], value: BaseException, traceback: TracebackType | None
    ) -> None:
        _LOG.critical("uncaught exception", exc_info=(kind, value, traceback))

    def log_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        _LOG.error(
            "uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value or args.exc_type(), args.exc_traceback),
        )

    for existing in saved_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    sys.excepthook = log_exception
    threading.excepthook = log_thread_exception
    try:
        yield log_file
    finally:
        sys.excepthook = saved_excepthook
        threading.excepthook = saved_thread_hook
        logging.captureWarnings(False)
        root.removeHandler(handler)
        handler.close()
        for existing in saved_handlers:
            root.addHandler(existing)
        root.setLevel(saved_level)


__all__ = ["terminal_quiet"]
