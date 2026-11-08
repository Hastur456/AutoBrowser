"""The stand's dispatcher: triggers in, one run at a time out (one worker, design V1).

``fire`` decides synchronously what happens to a trigger — queued, coalesced or skipped
(disabled job, cooldown, day budget, full queue, shutdown) — and says so in an event. ``run``
executes queued runs on the worker: the main task under the job's run profile, then the
report into the ledger, the notifications and the job state. A failure of one run never stops
the stand.
"""

from __future__ import annotations

import asyncio
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
from src.serve.profiles import DayLeft, main_profile
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


@dataclass
class _Run:
    run_id: str
    job_id: str
    trigger: TriggerRef
    queued_at: datetime
    #: A free task (``POST /v1/tasks``) instead of the job's task.
    task: str | None = None
    profile: RunProfile | None = None


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


class Dispatcher:
    """Queue, coalescing, cooldown and day budgets over one worker."""

    def __init__(
        self,
        settings: ServeSettings,
        worker: WorkerPort,
        *,
        ledger: Ledger,
        states: JobStateStore,
        notifier: Notifier,
        events: EventEmitter,
        tally: RunTally,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._ledger = ledger
        self._states = states
        self._notifier = notifier
        self._events = events
        self._tally = tally
        self._clock = clock
        self._queue: deque[_Run] = deque()
        self._wakeup = asyncio.Event()
        self._running: _Run | None = None
        self._current: asyncio.Task[None] | None = None
        self._stopping = False
        self._runs_in_session = 0
        self._serve_state = states.load_serve()

    # ------------------------------------------------------------------ triggers

    @property
    def settings(self) -> ServeSettings:
        return self._settings

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

    async def submit_task(self, task: str, profile: RunProfile, trigger: TriggerRef) -> FireResult:
        """Queue a free task (``serve.free_tasks``) under ``profile``."""

        run = _Run(
            run_id=str(profile.labels.get("run_id") or _run_id()),
            job_id="",
            trigger=trigger,
            queued_at=self._clock.now(),
            task=task,
            profile=profile,
        )
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

    def _refusal(self, job: JobSpec, run: _Run) -> str:
        if self._stopping:
            return "shutdown"
        if not job.enabled:
            return "disabled"
        mode = job.coalesce or self._settings.coalesce
        queued = any(item.job_id == run.job_id for item in self._queue)
        running = self._running is not None and self._running.job_id == run.job_id
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
        """Execute queued runs until :meth:`stop`."""

        while not self._stopping:
            if not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            await self._run_next()

    async def run_pending(self) -> None:
        """Execute every queued run, then return (tests, one-shot use)."""

        while self._queue and not self._stopping:
            await self._run_next()

    async def stop(self, grace_seconds: float) -> None:
        """Stop taking triggers, drop the queue, give the running run ``grace_seconds``, then
        cancel it."""

        self._stopping = True
        while self._queue:
            await self._skip(self._queue.popleft(), "shutdown")
        self._wakeup.set()
        current = self._current
        if current is None or current.done():
            return
        done, _ = await asyncio.wait({current}, timeout=grace_seconds)
        if not done:
            await self._worker.cancel()
            await asyncio.wait({current})

    async def _run_next(self) -> None:
        run = self._queue.popleft()
        self._running = run
        self._current = asyncio.create_task(self._execute(run))
        try:
            await self._current
        except Exception:  # noqa: BLE001 - a run never stops the stand
            _LOG.exception("run %s of %s failed outside its task", run.run_id, run.job_id)
        finally:
            self._running = None
            self._current = None

    async def _execute(self, run: _Run) -> None:
        started = self._clock.now()
        job = self._settings.jobs.get(run.job_id)
        budget: dict[str, Any] = {}
        try:
            await ensure_healthy(self._worker)
            result = await self._run_tasks(run, job, budget)
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
        )
        self._ledger.append(report)
        if run.job_id:
            state = self._states.load(run.job_id)
            state.last_finished_at = finished
            state.last_run_id = run.run_id
            self._after_run(run, result, state)
            self._save_state(run.job_id, state)
        self._emit("serve.run.finished", report.to_json())
        if result.tasks:
            await self._count_session_run()

    async def _run_tasks(self, run: _Run, job: JobSpec | None, budget: dict[str, Any]) -> _Result:
        labels = {
            "mode": "serve",
            "job_id": run.job_id,
            "run_id": run.run_id,
            "trigger": run.trigger.label(),
        }
        if run.task is not None:
            profile = run.profile or RunProfile(carry_context=False, labels=labels)
            budget["run"] = _budget_dict(profile)
            return await self._run_main(run.task, profile, "free")
        assert job is not None
        has_recording = job.recording is not None and self._worker.has_recording(job.recording)
        max_stage = job.max_stage or self._settings.default_max_stage
        if ladder_rank(max_stage) < ladder_rank("agent") and not has_recording:
            # Nothing may run without the model, and there is nothing to replay.
            return _Result(status="blocked", stop_reason="ladder_cap")
        profile = main_profile(
            job, self._settings, day_left=self.day_left(), labels=labels, has_recording=has_recording
        )
        budget["run"] = _budget_dict(profile)
        return await self._run_main(job.task, profile, "main")

    async def _run_main(self, task: str, profile: RunProfile, kind: TaskKind) -> _Result:
        outcome = await self._worker.run_outcome(task, profile=profile)
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
        )

    def _after_run(self, run: _Run, result: _Result, state: Any) -> None:
        """Hook for run kinds that update the job state further (the probe)."""

    async def _notify(self, run: _Run, job: JobSpec | None, result: _Result) -> tuple[str, ...]:
        notify_on = job.notify_on if job is not None else ["failure", "blocked", "budget", "success"]
        kinds: list[tuple[NotifyKind, str, str]] = list(result.notes)
        kind = _outcome_kind(result)
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

    async def _count_session_run(self) -> None:
        self._runs_in_session += 1
        if self._runs_in_session >= self._settings.session_max_runs:
            self._runs_in_session = 0
            try:
                await self._worker.reset()
            except Exception:  # noqa: BLE001 - the next run tries again
                _LOG.warning("the worker session was not recreated", exc_info=True)

    # ------------------------------------------------------------------ views

    def day_left(self) -> DayLeft:
        settings = self._settings
        totals = self._ledger.day_totals(self._ledger.day_of(self._clock.now()))
        return DayLeft(
            tokens=None if settings.daily_max_tokens is None else max(0, settings.daily_max_tokens - totals.tokens),
            cost=None if settings.daily_max_cost is None else max(0.0, settings.daily_max_cost - totals.cost),
        )

    def run_status(self, run_id: str) -> dict[str, Any] | None:
        """A queued or running run's state, else its report; ``None`` when unknown."""

        if self._running is not None and self._running.run_id == run_id:
            return {**self._payload(self._running), "state": "running"}
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
        return {
            "stopping": self._stopping,
            "running": None if self._running is None else self._payload(self._running),
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


__all__ = ["Dispatcher", "FireResult", "TriggerRef"]
