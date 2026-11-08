"""``ServeRuntime``: the stand's lifetime around one worker and its dispatcher.

Start the worker session (browser, MCP), check that every probe tool exists, emit
``serve.started``, run the dispatcher and the other services (the scheduler, the HTTP API)
until ``until`` finishes, then stop in order (design section 7.2): no new triggers, the queue
dropped, the running run given ``serve.shutdown_grace_seconds``, the worker shut down,
``serve.stopped``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from src.agent_loop.events import EventEmitter
from src.config import ServeSettings
from src.serve.dispatcher import Dispatcher
from src.serve.worker import WorkerPort

_SOURCE = "serve.runtime"
_LOG = logging.getLogger(__name__)

#: A long-running part of the stand (the scheduler); cancelled when the stand stops.
Service = Callable[[], Awaitable[Any]]


class ServeStartError(RuntimeError):
    """The stand cannot start as configured (the message says why, without secrets)."""


class ServeServiceError(RuntimeError):
    """A part of the stand (the dispatcher, the scheduler) died: the stand stops rather than
    keep answering ``/healthz`` while nothing runs (a container restart policy brings it back)."""


class ServeRuntime:
    def __init__(
        self,
        settings: ServeSettings,
        worker: WorkerPort,
        *,
        dispatcher: Dispatcher,
        events: EventEmitter,
        services: Sequence[Service] = (),
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._dispatcher = dispatcher
        self._events = events
        self._services = list(services)

    async def serve(self, *, until: Awaitable[Any]) -> None:
        """Run the stand until ``until`` finishes (or raises, or this task is cancelled)."""

        waiting = asyncio.ensure_future(until)
        tasks: list[asyncio.Task[Any]] = []
        try:
            await self._worker.start()
            await self._check_probe_tools()
            self._emit("serve.started", {
                "jobs": sorted(self._settings.jobs),
                "host": self._settings.host,
                "port": self._settings.port,
                "channels": list(self._settings.channels),
                "api_approvals": self._settings.api_approvals,
            })
            tasks = [asyncio.create_task(self._dispatcher.run(), name="dispatcher")]
            tasks += [
                asyncio.create_task(service(), name=getattr(service, "__qualname__", "service"))
                for service in self._services
            ]
            died: asyncio.Future[BaseException] = asyncio.get_running_loop().create_future()
            for task in tasks:
                task.add_done_callback(lambda done, died=died: _report_death(done, died))
            await asyncio.wait({waiting, died}, return_when=asyncio.FIRST_COMPLETED)
            if died.done():
                failure = died.result()
                raise ServeServiceError(
                    f"a stand service stopped: {type(failure).__name__}"
                ) from failure
            waiting.result()  # re-raise what ended the wait (a signal, an error)
        finally:
            waiting.cancel()
            with contextlib.suppress(BaseException):
                await waiting
            started = bool(tasks)
            await self._dispatcher.stop(self._settings.shutdown_grace_seconds)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            await self._worker.shutdown(self._settings.shutdown_grace_seconds)
            if started:
                self._emit("serve.stopped", {})

    async def _check_probe_tools(self) -> None:
        available = set(await self._worker.tool_names())
        missing = sorted(
            {
                f"{name}: {step.tool}"
                for name, job in self._settings.jobs.items()
                if job.probe is not None
                for step in job.probe.steps
                if step.tool not in available
            }
        )
        if missing:
            raise ServeStartError(
                f"probe steps name tools the session does not have ({', '.join(missing)}); "
                f"available: {', '.join(sorted(available)) or 'none'}"
            )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, source=_SOURCE, payload=payload)  # type: ignore[arg-type]


def _report_death(task: asyncio.Task[Any], died: asyncio.Future[BaseException]) -> None:
    if task.cancelled() or died.done():
        return
    failure = task.exception()
    if failure is not None:
        _LOG.error("stand service %s stopped: %s", task.get_name(), type(failure).__name__, exc_info=failure)
        died.set_result(failure)


__all__ = ["ServeRuntime", "ServeServiceError", "ServeStartError", "Service"]
