"""The session's own event loop on a background thread.

Every front end keeps the terminal (or a server) on its main thread and runs all session
coroutines -- start, tasks, MCP reconnects, the event stream -- on this one loop, so a slow
model or a blocking call in the agent stack never freezes input, and a slow renderer never
slows the agent.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from src.config import get_settings

T = TypeVar("T")


class RuntimeLoop:
    """Own one event loop for all async session operations."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="autobrowser-runtime-loop",
            daemon=True,
        )
        self._thread.start()
        self._started.wait()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio loop the session runs on."""

        return self._loop

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule ``coro`` on the session loop; thread-safe."""

        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the session loop and block the calling thread for its result."""

        return self.submit(coro).result()

    def stop(self) -> None:
        """Stop the loop and wait (bounded by ``ui.shutdown_timeout_seconds``) for the thread."""

        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=get_settings().ui.shutdown_timeout_seconds)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()


__all__ = ["RuntimeLoop"]
