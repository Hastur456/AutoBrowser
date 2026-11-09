"""The stand's workers (design V2): ``serve.workers`` sessions, each with its own browser,
profile, CDP port and MCP processes, behind one :class:`WorkerPool`.

A worker with pinned jobs (``JobSpec.worker``) is reserved: it runs only those (a profile
with logins must not serve a free task from a chat). Every other job takes the free
unreserved worker with the lowest index. One worker is the stand of phase 3, unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.config import ServeSettings
from src.serve.worker import WorkerPort


@dataclass
class WorkerSlot:
    """One worker and what the dispatcher keeps about it."""

    index: int
    worker: WorkerPort
    #: Jobs pinned to this worker; non-empty: only they run here.
    pinned: frozenset[str] = frozenset()
    busy: bool = False
    runs_in_session: int = 0

    @property
    def label(self) -> str:
        return f"w{self.index}"


class WorkerStartError(RuntimeError):
    """A worker's session did not start (the message names the worker, not a secret)."""


class WorkerPool:
    def __init__(
        self, workers: Sequence[WorkerPort], *, pinned: Mapping[int, frozenset[str]] | None = None
    ) -> None:
        if not workers:
            raise ValueError("a worker pool needs at least one worker")
        pins = pinned or {}
        self._slots = tuple(
            WorkerSlot(index, worker, frozenset(pins.get(index, frozenset())))
            for index, worker in enumerate(workers)
        )

    @classmethod
    def of(cls, worker: WorkerPort) -> WorkerPool:
        """One worker (the phase 3 stand, and tests)."""

        return cls([worker])

    @classmethod
    def from_settings(cls, workers: Sequence[WorkerPort], settings: ServeSettings) -> WorkerPool:
        """The pool with the pins of ``settings.jobs``."""

        pinned: dict[int, set[str]] = {}
        for job_id, job in settings.jobs.items():
            if job.worker is not None:
                pinned.setdefault(job.worker, set()).add(job_id)
        return cls(workers, pinned={index: frozenset(jobs) for index, jobs in pinned.items()})

    @property
    def size(self) -> int:
        return len(self._slots)

    def slots(self) -> tuple[WorkerSlot, ...]:
        return self._slots

    def slot(self, index: int) -> WorkerSlot:
        return self._slots[index]

    def slot_for(self, job_id: str, pin: int | None) -> WorkerSlot | None:
        """The free slot a run may use: its pinned worker, else the lowest-index free
        unreserved one; ``None`` while none is free."""

        if pin is not None:
            slot = self._slots[pin] if 0 <= pin < len(self._slots) else None
            return slot if slot is not None and not slot.busy else None
        return next((slot for slot in self._slots if not slot.pinned and not slot.busy), None)

    # ------------------------------------------------------------------ lifetime

    async def start(self) -> Any:
        """Start the workers one after another (a failure names the worker)."""

        views = []
        for slot in self._slots:
            try:
                views.append(await slot.worker.start())
            except Exception as exc:
                if self.size == 1:
                    raise
                raise WorkerStartError(f"worker {slot.label} did not start: {type(exc).__name__}") from exc
        return views[0] if self.size == 1 else views

    async def tool_names(self) -> tuple[str, ...]:
        """The tools of worker ``w0``: every worker runs the same configuration."""

        return await self._slots[0].worker.tool_names()

    async def shutdown(self, timeout_seconds: float) -> None:
        """Shut every worker down at once; one failing never keeps the others running."""

        if self.size == 1:
            await self._slots[0].worker.shutdown(timeout_seconds)
            return
        await asyncio.gather(
            *(slot.worker.shutdown(timeout_seconds) for slot in self._slots), return_exceptions=True
        )


__all__ = ["WorkerPool", "WorkerSlot", "WorkerStartError"]
