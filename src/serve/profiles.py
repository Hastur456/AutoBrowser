"""The run profiles of stand tasks: isolated (no carried context), scoped to the job's domains,
capped by the ladder and by the smaller of the job budget and what is left of the day."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.config import JobSpec, RunBudgetSpec, ServeSettings
from src.contracts import PlaybackRequest, RunBudget, RunProfile


@dataclass(frozen=True)
class DayLeft:
    """What the day budgets still allow; ``None``: no day cap."""

    tokens: int | None = None
    cost: float | None = None


def _smaller(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def run_budget(spec: RunBudgetSpec | None, settings: ServeSettings, day_left: DayLeft) -> RunBudget:
    """The job's budget (else ``serve.default_run_budget``) narrowed by the day's rest.

    Model calls come from the job alone: ``daily_max_runs`` counts runs, not calls.
    """

    spec = spec or settings.default_run_budget
    tokens = _smaller(spec.max_tokens, day_left.tokens)
    return RunBudget(
        max_model_calls=spec.max_model_calls,
        max_tokens=None if tokens is None else int(tokens),
        max_cost=_smaller(spec.max_cost, day_left.cost),
    )


def main_profile(
    job: JobSpec,
    settings: ServeSettings,
    *,
    day_left: DayLeft,
    labels: Mapping[str, str],
    has_recording: bool,
) -> RunProfile:
    """The main task of a run: replay the job's recording when it exists, else the agent."""

    playback = (
        PlaybackRequest(recording=job.recording)
        if job.recording is not None and has_recording
        else None
    )
    return RunProfile(
        playback=playback,
        max_stage=job.max_stage or settings.default_max_stage,
        carry_context=False,
        record_as=job.recording if job.record_on_success else None,
        budget=run_budget(job.budget, settings, day_left),
        allowed_domains=tuple(job.domains),
        labels=dict(labels),
    )


def free_task_profile(
    settings: ServeSettings, *, day_left: DayLeft, labels: Mapping[str, str]
) -> RunProfile:
    """A free task (``POST /v1/tasks``): the stand's defaults and ``free_task_domains``."""

    return RunProfile(
        max_stage=settings.default_max_stage,
        carry_context=False,
        budget=run_budget(None, settings, day_left),
        allowed_domains=tuple(settings.free_task_domains),
        labels=dict(labels),
    )


__all__ = ["DayLeft", "free_task_profile", "main_profile", "run_budget"]
