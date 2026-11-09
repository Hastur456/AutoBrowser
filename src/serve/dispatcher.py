"""The stand's dispatcher: triggers in, runs out on a pool of workers (design V1/V2).

``fire`` decides synchronously what happens to a trigger — queued, coalesced or skipped
(disabled job, cooldown, day budget, full queue, shutdown) — and says so in an event. ``run``
starts queued runs on free workers, in queue order: the main task under the job's run
profile, then the report into the ledger, the notifications and the job state. A failure of
one run never stops the stand.

With several workers (``serve.workers``) runs go in parallel, and:

- two runs of one job, or two runs replaying or recording one recording, never run at once
  (exclusion keys ``job:<id>``, ``recording:<name>``);
- a pinned job (``JobSpec.worker``) waits for its worker; a worker with pinned jobs runs only
  those;
- a running run holds its budget (tokens, money) out of what is left of the day, so parallel
  runs together never get more than the day has; a run without room waits in the queue.

Everything runs on one asyncio loop and every file write is synchronous, so the ledger and
the state files never interleave. With one worker the stand behaves exactly as in phase 3.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.agent_loop.events import EventEmitter
from src.config import JobSpec, NotifyKind, ServeSettings
from src.contracts import RunProfile, ladder_rank
from src.serve.clock import Clock
from src.serve.ledger import Ledger
from src.serve.notify import Notification, Notifier
from src.serve.pool import WorkerPool, WorkerSlot
from src.serve.profiles import DayLeft, free_task_profile, main_profile, probe_profile
from src.serve.reports import RunReport, RunState, RunStatus, TaskKind, TaskSummary
from src.serve.state import JobStateStore
from src.serve.tally import RunTally
from src.serve.worker import WorkerPort, ensure_healthy
from src.ui.views import TaskOutcome

_LOG = logging.getLogger(__name__)
_SOURCE = "serve.dispatcher"

#: TaskOutcome status -> run report status.
_STATUSES: dict[str, RunStatus] = {
    "completed": "completed",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "failed": "failed",
}


@dataclass(frozen=True)
class TriggerRef:
    """What fired: a trigger of the job (``interval:hourly``) or an API call (``api:manual``)."""

    type: str
    id: str

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "id": self.id}

    def label(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True)
class FireResult:
    run_id: str
    state: RunState
    reason: str = ""


@dataclass(frozen=True)
class CancelResult:
    """What a cancel did: queued runs dropped, running runs asked to stop."""

    cancelled: tuple[str, ...] = ()
    stopping: tuple[str, ...] = ()
    #: Without a target and with several runs running: they, none of them cancelled.
    running: tuple[str, ...] = ()


@dataclass
class _Run:
    run_id: str
    job_id: str
    trigger: TriggerRef
    queued_at: datetime
    #: A free task (``POST /v1/tasks``) instead of the job's task.
    task: str | None = None
    #: Cancelled while running: no further task of the run starts.
    cancelled: bool = False


@dataclass(frozen=True)
class _Reservation:
    """The budget a running run holds out of the day (``None``: that cap is not set)."""

    tokens: int | None
    cost: float | None


@dataclass
class _Result:
    status: RunStatus
    stop_reason: str = ""
    stage: str = ""
    answer: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    tasks: list[TaskSummary] = field(default_factory=list)
    playback: dict[str, Any] = field(default_factory=dict)
    recording_updated: bool = False
    changed: bool | None = None
    #: Extra notification kinds the run itself raised (a probe's ``change``).
    notes: list[tuple[NotifyKind, str, str]] = field(default_factory=list)
    #: The highest model tier a task of the run reached (``None``: no escalation).
    tier: int | None = None


class Dispatcher:
    """Queue, coalescing, cooldown and day budgets over a pool of workers (one by default)."""

    def __init__(
        self,
        settings: ServeSettings,
        workers: WorkerPort | WorkerPool,
        *,
        ledger: Ledger,
        states: JobStateStore,
        notifier: Notifier,
        events: EventEmitter,
        tally: RunTally,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._pool = workers if isinstance(workers, WorkerPool) else WorkerPool.of(workers)
        self._ledger = ledger
        self._states = states
        self._notifier = notifier
        self._events = events
        self._tally = tally
        self._clock = clock
        self._queue: deque[_Run] = deque()
        self._wakeup = asyncio.Event()
        self._stopped = asyncio.Event()
        #: Slot index -> its run, from leaving the queue to the end (exclusion keys).
        self._held: dict[int, _Run] = {}
        #: Slot index -> its run once past the start checks (coalescing, status, cancel).
        self._running: dict[int, _Run] = {}
        self._current: dict[int, asyncio.Task[None]] = {}
        #: Run id -> the budget it holds out of the day (only with several workers).
        self._reserved: dict[str, _Reservation] = {}
        self._stopping = False
        self._serve_state = states.load_serve()

    # ------------------------------------------------------------------ triggers

    @property
    def settings(self) -> ServeSettings:
        return self._settings

    @property
    def notifier(self) -> Notifier:
        return self._notifier

    @property
    def pool(self) -> WorkerPool:
        return self._pool

    def running(self) -> tuple[str, str] | None:
        """``(job_id, run_id)`` of the run in progress on the lowest-index worker, if any."""

        for index in sorted(self._running):
            return self.running_on(index)
        return None

    def running_on(self, index: int) -> tuple[str, str] | None:
        """``(job_id, run_id)`` of the run in progress on worker ``index``, if any."""

        run = self._running.get(index)
        return None if run is None else (run.job_id, run.run_id)

    async def fire(self, job_id: str, trigger: TriggerRef) -> FireResult:
        """Queue a run of ``job_id``, or say why not. ``KeyError`` for an unknown job."""

        job = self._settings.jobs[job_id]
        run = _Run(run_id=_run_id(), job_id=job_id, trigger=trigger, queued_at=self._clock.now())
        self._emit("serve.trigger.fired", self._payload(run))
        reason = self._refusal(job, run)
        if reason == "coalesced":
            await self._skip(run, reason)
            return FireResult(run.run_id, "coalesced", reason)
        if reason:
            await self._skip(run, reason, job=job)
            return FireResult(run.run_id, "skipped", reason)
        self._enqueue(run)
        return FireResult(run.run_id, "queued")

    async def submit_task(self, task: str, trigger: TriggerRef) -> FireResult:
        """Queue a free task (``serve.free_tasks``); its profile is built when it starts, so
        its budget is what is left of the day then, not now."""

        run = _Run(run_id=_run_id(), job_id="", trigger=trigger, queued_at=self._clock.now(), task=task)
        self._emit("serve.trigger.fired", self._payload(run))
        reason = "shutdown" if self._stopping else self._day_refusal() or self._queue_refusal()
        if reason:
            await self._skip(run, reason)
            return FireResult(run.run_id, "skipped", reason)
        self._enqueue(run)
        return FireResult(run.run_id, "queued")

    def new_run_id(self) -> str:
        return _run_id()

    async def skip(self, job_id: str, trigger: TriggerRef, reason: str) -> None:
        """Record a trigger that did not fire for a reason of its own (the scheduler's
        ``min_interval``): counted in the job state and reported as ``serve.run.skipped``."""

        run = _Run(run_id=_run_id(), job_id=job_id, trigger=trigger, queued_at=self._clock.now())
        await self._skip(run, reason)

    async def cancel(self, target: str = "") -> CancelResult:
        """Cancel by run id or job id: queued runs are dropped (``skipped{cancelled}``), a
        running one is asked to stop. Without ``target``: the running run only."""

        dropped = [
            run for run in self._queue if target and target in (run.run_id, run.job_id)
        ]
        for run in dropped:
            self._queue.remove(run)
            await self._skip(run, "cancelled")
        running = dict(self._running)
        if not target and len(running) > 1:
            return CancelResult(running=tuple(run.run_id for _, run in sorted(running.items())))
        stopping: list[str] = []
        for index, run in sorted(running.items()):
            if target and target not in (run.run_id, run.job_id):
                continue
            run.cancelled = True
            await self._pool.slot(index).worker.cancel()
            stopping.append(run.run_id)
        return CancelResult(tuple(run.run_id for run in dropped), tuple(stopping))

    def _refusal(self, job: JobSpec, run: _Run) -> str:
        if self._stopping:
            return "shutdown"
        if not job.enabled:
            return "disabled"
        mode = job.coalesce or self._settings.coalesce
        queued = any(item.job_id == run.job_id for item in self._queue)
        running = any(item.job_id == run.job_id for item in self._running.values())
        if queued or (running and mode == "drop"):
            return "coalesced"
        state = self._states.load(run.job_id)
        cooldown = (
            job.cooldown_seconds if job.cooldown_seconds is not None else self._settings.cooldown_seconds
        )
        if cooldown and state.last_finished_at is not None:
            if (run.queued_at - state.last_finished_at).total_seconds() < cooldown:
                return "cooldown"
        return self._day_refusal() or self._queue_refusal()

    def _day_refusal(self) -> str:
        settings = self._settings
        totals = self._ledger.day_totals(self._ledger.day_of(self._clock.now()))
        if (
            (settings.daily_max_runs is not None and totals.runs >= settings.daily_max_runs)
            or (settings.daily_max_tokens is not None and totals.tokens >= settings.daily_max_tokens)
            or (settings.daily_max_cost is not None and totals.cost >= settings.daily_max_cost)
        ):
            return "budget"
        return ""

    def _queue_refusal(self) -> str:
        return "queue_full" if len(self._queue) >= self._settings.queue_size else ""

    def _enqueue(self, run: _Run) -> None:
        self._queue.append(run)
        self._emit("serve.run.queued", {**self._payload(run), "position": len(self._queue)})
        self._wakeup.set()

    async def _skip(self, run: _Run, reason: str, *, job: JobSpec | None = None) -> None:
        if run.job_id:
            state = self._states.load(run.job_id)
            state.counters[reason] = state.counters.get(reason, 0) + 1
            self._save_state(run.job_id, state)
        self._emit("serve.run.skipped", {**self._payload(run), "reason": reason})
        if reason == "budget" and job is not None:
            await self._notify_budget_once(run, job)

    async def _notify_budget_once(self, run: _Run, job: JobSpec) -> None:
        today = self._ledger.day_of(self._clock.now()).isoformat()
        if self._serve_state.budget_notified_day == today or "budget" not in job.notify_on:
            return
        self._serve_state.budget_notified_day = today
        try:
            self._states.save_serve(self._serve_state)
        except OSError:
            _LOG.warning("the stand state was not saved", exc_info=True)
        totals = self._ledger.day_totals(self._ledger.day_of(self._clock.now()))
        await self._notifier.notify(
            Notification(
                kind="budget",
                job_id=run.job_id,
                run_id=run.run_id,
                title="Day budget reached: runs are skipped until tomorrow",
                body=f"runs {totals.runs}, tokens {totals.tokens}, cost {totals.cost:.4f}",
                cost=totals.cost,
            )
        )

    # ------------------------------------------------------------------ running

    async def run(self) -> None:
        """Start queued runs on free workers until :meth:`stop`."""

        while not self._stopping:
            self._wakeup.clear()
            await self._start_eligible()
            if self._stopping:
                break
            if not self._wakeup.is_set():
                await self._wakeup.wait()

    async def run_pending(self) -> None:
        """Execute every queued run, then return (tests, one-shot use)."""

        while not self._stopping:
            await self._start_eligible()
            if not self._current:
                return
            await asyncio.wait(set(self._current.values()), return_when=asyncio.FIRST_COMPLETED)

    async def stop(self, grace_seconds: float) -> None:
        """Stop taking triggers, drop the queue, give the running runs ``grace_seconds``, then
        cancel them."""

        self._stopping = True
        self._stopped.set()
        while self._queue:
            await self._skip(self._queue.popleft(), "shutdown")
        self._wakeup.set()
        tasks = {task for task in self._current.values() if not task.done()}
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        if not pending:
            return
        for index in sorted(self._running):
            await self._pool.slot(index).worker.cancel()
        _, pending = await asyncio.wait(pending, timeout=grace_seconds)
        if pending:
            # Nothing of the session was running to cancel (a reset, a reconnect): stop the
            # runs themselves; their reports are lost rather than the shutdown.
            for task in pending:
                task.cancel()
            await asyncio.wait(pending)

    async def _start_eligible(self) -> None:
        """Start, in queue order, every queued run that may start now: its exclusion keys are
        free, a worker it may use is free and the day has room after the running runs."""

        for run in list(self._queue):
            if self._stopping:
                return
            if run not in self._queue:
                continue
            job = self._settings.jobs.get(run.job_id)
            if self._excluded(run, job):
                continue
            slot = self._pool.slot_for(run.job_id, job.worker if job is not None else None)
            if slot is None:
                continue
            if self._day_refusal():
                # The day cap filled up while the run waited.
                self._queue.remove(run)
                await self._skip(run, "budget", job=job)
                continue
            if not self._day_room():
                continue
            self._queue.remove(run)
            self._start(slot, run)

    def _excluded(self, run: _Run, job: JobSpec | None) -> bool:
        keys = _exclusion_keys(run, job)
        return any(
            keys & _exclusion_keys(other, self._settings.jobs.get(other.job_id))
            for other in self._held.values()
        )

    def _day_room(self) -> bool:
        """Room in the day once the running runs are counted (always, with nothing running)."""

        if not self._held:
            return True
        settings = self._settings
        totals = self._ledger.day_totals(self._ledger.day_of(self._clock.now()))
        if settings.daily_max_runs is not None and totals.runs + len(self._held) >= settings.daily_max_runs:
            return False
        left = self.day_left()
        return not (
            (left.tokens is not None and left.tokens <= 0) or (left.cost is not None and left.cost <= 0)
        )

    def _start(self, slot: WorkerSlot, run: _Run) -> None:
        slot.busy = True
        self._held[slot.index] = run
        # Now, not in the task: the next run of this same scan must see the budget held.
        self._reserve(run)
        self._current[slot.index] = asyncio.create_task(self._run_on(slot, run))

    async def _run_on(self, slot: WorkerSlot, run: _Run) -> None:
        try:
            reason = await self._start_refusal(run)
            if reason:
                await self._skip(run, reason, job=self._settings.jobs.get(run.job_id))
                return
            self._running[slot.index] = run
            await self._execute(slot, run)
        except Exception:  # noqa: BLE001 - a run never stops the stand
            _LOG.exception("run %s of %s failed outside its task", run.run_id, run.job_id)
        finally:
            self._reserved.pop(run.run_id, None)
            self._running.pop(slot.index, None)
            self._held.pop(slot.index, None)
            self._current.pop(slot.index, None)
            slot.busy = False
            self._wakeup.set()

    async def _start_refusal(self, run: _Run) -> str:
        """Why a run taken off the queue may not start after all: the stand stopped while it
        waited out its job's cooldown."""

        job = self._settings.jobs.get(run.job_id)
        if job is None:
            return ""
        cooldown = job.cooldown_seconds if job.cooldown_seconds is not None else self._settings.cooldown_seconds
        last = self._states.load(run.job_id).last_finished_at
        if cooldown and last is not None:
            remaining = cooldown - (self._clock.now() - last).total_seconds()
            if remaining > 0 and not await self._wait_unless_stopping(remaining):
                return "shutdown"
        return ""

    def _reserve(self, run: _Run) -> None:
        """Hold the run's budget out of the day while it runs (several workers only)."""

        if self._pool.size == 1:
            return
        job = self._settings.jobs.get(run.job_id)
        spec = job.budget if job is not None and job.budget is not None else self._settings.default_run_budget
        left = self.day_left(exclude=run.run_id)
        self._reserved[run.run_id] = _Reservation(
            tokens=_smaller(spec.max_tokens, left.tokens),
            cost=_smaller(spec.max_cost, left.cost),
        )

    def _run_day_left(self, run: _Run) -> DayLeft:
        """What of the day the run's profile may use: its reservation, else the day's rest."""

        held = self._reserved.get(run.run_id)
        if held is None:
            return self.day_left()
        return DayLeft(tokens=held.tokens, cost=held.cost)

    async def _wait_unless_stopping(self, seconds: float) -> bool:
        """Sleep ``seconds`` on the stand's clock; ``False`` when the stand stopped meanwhile."""

        sleeping = asyncio.ensure_future(self._clock.sleep(seconds))
        stopping = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeping, stopping):
                task.cancel()
        return not self._stopping

    async def _execute(self, slot: WorkerSlot, run: _Run) -> None:
        started = self._clock.now()
        job = self._settings.jobs.get(run.job_id)
        budget: dict[str, Any] = {}
        try:
            await ensure_healthy(slot.worker)
            result = await self._run_tasks(slot.worker, run, job, budget)
        except Exception as exc:  # noqa: BLE001 - the session could not run the task
            result = _Result(status="failed", error=f"{type(exc).__name__}: {exc}")
        finished = self._clock.now()
        notified = await self._notify(run, job, result)
        totals = self._ledger.day_totals(self._ledger.day_of(finished))
        report = RunReport(
            run_id=run.run_id,
            job_id=run.job_id,
            trigger=run.trigger.as_dict(),
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=int((finished - started).total_seconds() * 1000),
            status=result.status,
            stop_reason=result.stop_reason,
            stage_reached=result.stage,
            tasks=tuple(result.tasks),
            changed=result.changed,
            playback=result.playback,
            usage=result.usage,
            budget={**budget, "day_spent_cost": totals.cost + float(result.usage.get("cost") or 0.0)},
            recording_updated=result.recording_updated,
            notified=notified,
            error=result.error,
            worker=slot.index if self._pool.size > 1 else None,
            tier_reached=result.tier,
        )
        self._ledger.append(report)
        if run.job_id:
            state = self._states.load(run.job_id)
            state.last_finished_at = finished
            state.last_run_id = run.run_id
            self._save_state(run.job_id, state)
        self._emit("serve.run.finished", report.to_json())
        if result.tasks:
            await self._count_session_run(slot)

    async def _run_tasks(
        self, worker: WorkerPort, run: _Run, job: JobSpec | None, budget: dict[str, Any]
    ) -> _Result:
        labels = {
            "mode": "serve",
            "job_id": run.job_id,
            "run_id": run.run_id,
            "trigger": run.trigger.label(),
        }
        if run.task is not None:
            profile = free_task_profile(self._settings, day_left=self._run_day_left(run), labels=labels)
            budget["run"] = _budget_dict(profile)
            return await self._run_main(worker, run.task, profile, "free")
        assert job is not None
        has_recording = job.recording is not None and worker.has_recording(job.recording)
        max_stage = job.max_stage or self._settings.default_max_stage
        if ladder_rank(max_stage) < ladder_rank("agent") and not has_recording:
            # Nothing may run without the model, and there is nothing to replay.
            return _Result(status="blocked", stop_reason="ladder_cap")
        probe: _Result | None = None
        fingerprint = ""
        if job.probe is not None:
            probe, fingerprint = await self._run_probe(worker, run, job, labels)
            if probe.status != "completed":
                return probe
            if probe.changed is False:
                return probe
            if self._stopping or run.cancelled:
                reason = "shutdown" if self._stopping else "cancelled"
                probe.status, probe.stop_reason, probe.changed = "cancelled", reason, True
                return probe
        profile = main_profile(
            job, self._settings, day_left=self._run_day_left(run), labels=labels, has_recording=has_recording
        )
        budget["run"] = _budget_dict(profile)
        main = await self._run_main(worker, job.task, profile, "main")
        if probe is None:
            return main
        if main.status == "completed":
            # Only a handled change moves the fingerprint: a failed main task retries next tick.
            state = self._states.load(run.job_id)
            previous = state.last_region
            state.last_digest = fingerprint
            state.last_region = probe.answer[: self._settings.notify_max_chars]
            self._save_state(run.job_id, state)
            main.notes.append((
                "change",
                f"{run.job_id}: changed",
                f"now: {probe.answer}\nwas: {previous or '(first probe)'}",
            ))
        main.tasks = probe.tasks + main.tasks
        main.usage = _add_usage(probe.usage, main.usage)
        main.changed = True
        main.tier = _higher(probe.tier, main.tier)
        return main

    async def _run_probe(
        self, worker: WorkerPort, run: _Run, job: JobSpec, labels: dict[str, str]
    ) -> tuple[_Result, str]:
        """Stage 0: ``(result, fingerprint)``; ``result.changed`` says whether the main task runs."""

        outcome = await worker.run_outcome(job.task, profile=probe_profile(run.job_id, job, labels=labels))
        result = self._result(outcome, "probe")
        if result.status != "completed":
            result.status = "failed"
            result.stop_reason = result.stop_reason or f"probe_{outcome.status}"
            result.error = result.error or f"the probe ended {outcome.status}"
            return result, ""
        fingerprint = "sha256:" + hashlib.sha256(result.answer.encode("utf-8")).hexdigest()
        if fingerprint == self._states.load(run.job_id).last_digest:
            result.status = "unchanged"
            result.stop_reason = "unchanged"
            result.stage = "probe"
            result.changed = False
            return result, fingerprint
        result.changed = True
        return result, fingerprint

    async def _run_main(
        self, worker: WorkerPort, task: str, profile: RunProfile, kind: TaskKind
    ) -> _Result:
        outcome = await worker.run_outcome(task, profile=profile)
        return self._result(outcome, kind)

    def _result(self, outcome: TaskOutcome, kind: TaskKind) -> _Result:
        tally = self._tally.take(outcome.task_id)
        return _Result(
            status=_STATUSES.get(outcome.status, "failed"),
            stop_reason=outcome.stop_reason,
            stage=outcome.stage,
            answer=outcome.final_answer,
            error=outcome.error,
            usage=_usage_dict(outcome),
            tasks=[
                TaskSummary(
                    task_id=outcome.task_id,
                    kind=kind,
                    status=outcome.status,
                    stop_reason=outcome.stop_reason,
                    stage=outcome.stage,
                )
            ],
            playback=tally.playback(),
            recording_updated=tally.recording_saved,
            tier=tally.tier_reached,
        )

    async def _notify(self, run: _Run, job: JobSpec | None, result: _Result) -> tuple[str, ...]:
        notify_on = job.notify_on if job is not None else ["failure", "blocked", "budget", "success"]
        kinds: list[tuple[NotifyKind, str, str]] = list(result.notes)
        kind = None if result.status == "unchanged" else _outcome_kind(result)
        if kind is not None:
            kinds.append((kind, f"{run.job_id or 'task'}: {result.status}", _body(result)))
        delivered: list[str] = []
        for note_kind, title, body in kinds:
            if note_kind not in notify_on:
                continue
            names = await self._notifier.notify(
                Notification(
                    kind=note_kind,
                    job_id=run.job_id,
                    run_id=run.run_id,
                    title=title,
                    body=body,
                    cost=_known_cost(result.usage),
                )
            )
            delivered.extend(name for name in names if name not in delivered)
        return tuple(delivered)

    async def _count_session_run(self, slot: WorkerSlot) -> None:
        slot.runs_in_session += 1
        if slot.runs_in_session >= self._settings.session_max_runs:
            slot.runs_in_session = 0
            try:
                await slot.worker.reset()
            except Exception:  # noqa: BLE001 - the next run tries again
                _LOG.warning("the session of worker %s was not recreated", slot.label, exc_info=True)

    # ------------------------------------------------------------------ views

    def day_left(self, *, exclude: str = "") -> DayLeft:
        """What is left of the day: the ledger's totals and the budgets the running runs hold
        (all but ``exclude``)."""

        settings = self._settings
        totals = self._ledger.day_totals(self._ledger.day_of(self._clock.now()))
        held = [item for run_id, item in self._reserved.items() if run_id != exclude]
        tokens = totals.tokens + sum(item.tokens or 0 for item in held)
        cost = totals.cost + sum(item.cost or 0.0 for item in held)
        return DayLeft(
            tokens=None if settings.daily_max_tokens is None else max(0, settings.daily_max_tokens - tokens),
            cost=None if settings.daily_max_cost is None else max(0.0, settings.daily_max_cost - cost),
        )

    def run_status(self, run_id: str) -> dict[str, Any] | None:
        """A queued or running run's state, else its report; ``None`` when unknown."""

        for run in self._running.values():
            if run.run_id == run_id:
                return {**self._payload(run), "state": "running"}
        for run in self._queue:
            if run.run_id == run_id:
                return {**self._payload(run), "state": "queued"}
        report = self._ledger.find(run_id)
        return report.to_json() if report is not None else None

    def status(self) -> dict[str, Any]:
        settings = self._settings
        day = self._ledger.day_of(self._clock.now())
        totals = self._ledger.day_totals(day)
        jobs: dict[str, Any] = {}
        for job_id, job in settings.jobs.items():
            state = self._states.load(job_id)
            jobs[job_id] = {
                "enabled": job.enabled,
                "triggers": [trigger.id for trigger in job.triggers],
                "last_run_id": state.last_run_id,
                "last_finished_at": (
                    state.last_finished_at.isoformat() if state.last_finished_at else None
                ),
                "counters": dict(state.counters),
            }
        lowest = next((run for _, run in sorted(self._running.items())), None)
        body: dict[str, Any] = {
            "stopping": self._stopping,
            "running": None if lowest is None else self._payload(lowest),
            "queue": [self._payload(run) for run in self._queue],
            "day": {
                "date": day.isoformat(),
                "runs": totals.runs,
                "tokens": totals.tokens,
                "cost": totals.cost,
                "max_runs": settings.daily_max_runs,
                "max_tokens": settings.daily_max_tokens,
                "max_cost": settings.daily_max_cost,
            },
            "jobs": jobs,
        }
        if self._pool.size > 1:
            body["workers"] = [
                {
                    "index": slot.index,
                    "pinned": sorted(slot.pinned),
                    "running": (
                        self._payload(self._running[slot.index]) if slot.index in self._running else None
                    ),
                }
                for slot in self._pool.slots()
            ]
        return body

    # ------------------------------------------------------------------ helpers

    def _payload(self, run: _Run) -> dict[str, Any]:
        return {"run_id": run.run_id, "job_id": run.job_id, "trigger": run.trigger.as_dict()}

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, source=_SOURCE, payload=payload)  # type: ignore[arg-type]

    def _save_state(self, job_id: str, state: Any) -> None:
        try:
            self._states.save(job_id, state)
        except OSError:
            _LOG.warning("state of job %s was not saved", job_id, exc_info=True)


def _run_id() -> str:
    return f"run-{uuid4().hex}"


def _exclusion_keys(run: _Run, job: JobSpec | None) -> set[str]:
    """What two runs may not hold at once: their job, the recording they replay or record."""

    keys = {f"job:{run.job_id}"} if run.job_id else set()
    if job is not None and job.recording is not None:
        keys.add(f"recording:{job.recording}")
    return keys


def _smaller(first: Any, second: Any) -> Any:
    """The smaller of two optional caps (``None`` is no cap)."""

    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def _higher(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _outcome_kind(result: _Result) -> NotifyKind | None:
    if result.status == "completed":
        return "success"
    if result.status == "failed":
        return "failure"
    if result.status == "blocked":
        return "budget" if result.stop_reason == "budget_exhausted" else "blocked"
    return None


def _body(result: _Result) -> str:
    if result.status == "completed":
        return result.answer
    return result.error or result.answer or result.stop_reason


def _usage_dict(outcome: TaskOutcome) -> dict[str, Any]:
    usage = outcome.usage
    if usage is None:
        return {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": None}
    return {
        "model_calls": usage.model_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost": usage.cost,
        "by_stage": {key: dict(value) for key, value in usage.by_stage.items()},
        "by_model": {key: dict(value) for key, value in usage.by_model.items()},
    }


def _add_usage(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """The usage of two tasks of one run; the cost is unknown once either is."""

    total = dict(second)
    for key in ("model_calls", "input_tokens", "output_tokens"):
        total[key] = int(first.get(key, 0) or 0) + int(second.get(key, 0) or 0)
    costs = (first.get("cost"), second.get("cost"))
    total["cost"] = None if None in costs else float(costs[0]) + float(costs[1])  # type: ignore[arg-type]
    return total


def _known_cost(usage: dict[str, Any]) -> float | None:
    cost = usage.get("cost")
    return None if cost is None else float(cost)


def _budget_dict(profile: RunProfile) -> dict[str, Any]:
    budget = profile.budget
    return {
        "max_model_calls": budget.max_model_calls,
        "max_tokens": budget.max_tokens,
        "max_cost": budget.max_cost,
    }


__all__ = ["CancelResult", "Dispatcher", "FireResult", "TriggerRef"]
