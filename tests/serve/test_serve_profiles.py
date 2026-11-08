"""Run profiles of stand tasks."""

from __future__ import annotations

from src.config import JobSpec, RunBudgetSpec, ServeSettings
from src.serve.profiles import DayLeft, free_task_profile, main_profile, run_budget


def test_the_run_budget_takes_the_smaller_of_job_and_day() -> None:
    settings = ServeSettings()
    spec = RunBudgetSpec(max_model_calls=4, max_tokens=500, max_cost=None)

    budget = run_budget(spec, settings, DayLeft(tokens=200, cost=0.3))

    assert (budget.max_model_calls, budget.max_tokens, budget.max_cost) == (4, 200, 0.3)


def test_without_day_caps_the_job_budget_stands() -> None:
    budget = run_budget(None, ServeSettings(), DayLeft())

    assert (budget.max_model_calls, budget.max_tokens, budget.max_cost) == (30, None, None)


def test_the_main_profile_never_carries_context() -> None:
    job = JobSpec(task="t", recording="rec", domains=["shop.example"])

    profile = main_profile(job, ServeSettings(), day_left=DayLeft(), labels={"run_id": "r"}, has_recording=True)

    assert profile.carry_context is False
    assert profile.playback.recording == "rec"
    assert profile.record_as is None
    assert profile.allowed_domains == ("shop.example",)
    assert profile.max_stage == "agent"


def test_a_free_task_gets_the_free_task_domains() -> None:
    settings = ServeSettings(free_tasks=True, free_task_domains=["example.com"])

    profile = free_task_profile(settings, day_left=DayLeft(), labels={})

    assert profile.allowed_domains == ("example.com",)
    assert profile.carry_context is False
    assert profile.budget.max_model_calls == 30
