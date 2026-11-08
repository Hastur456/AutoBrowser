"""What stops the stand from starting, checked before any browser runs.

``ServeSettings`` already validated its own fields; these checks need the rest of the
configuration (permissions, replay, prices) or the environment. Problems never quote a secret.
"""

from __future__ import annotations

import os

from src.config import Settings, TriggerSpec


def trigger_secret(trigger: TriggerSpec) -> str:
    """The shared secret of a webhook trigger (inline, or from its environment variable)."""

    if trigger.secret is not None:
        return trigger.secret.get_secret_value()
    if trigger.secret_env:
        return os.environ.get(trigger.secret_env, "")
    return ""


def startup_problems(settings: Settings) -> list[str]:
    """Everything that makes ``autobrowser serve`` refuse to start; ``[]`` when it may."""

    serve = settings.serve
    problems: list[str] = []
    if settings.permissions.mode == "bypass":
        problems.append(
            "permissions.mode bypass grants every approval; the stand refuses it "
            "(use default, read_only or dont_ask)"
        )
    if not settings.replay.enabled:
        for name, job in serve.jobs.items():
            if job.probe is not None or job.recording is not None:
                problems.append(
                    f"job {name!r} uses a probe or a recording, which needs replay.enabled "
                    f"(AUTOBROWSER_REPLAY__ENABLED=true)"
                )
    money = serve.daily_max_cost is not None or serve.default_run_budget.max_cost is not None or any(
        job.budget is not None and job.budget.max_cost is not None for job in serve.jobs.values()
    )
    if money and settings.llm.model not in settings.llm.prices:
        problems.append(
            f"a money cap is set but llm.prices has no price for the model {settings.llm.model!r}"
        )
    for name, job in serve.jobs.items():
        for trigger in job.triggers:
            if trigger.type == "webhook" and trigger.secret_env and not trigger_secret(trigger):
                problems.append(
                    f"job {name!r}, trigger {trigger.id!r}: the environment variable "
                    f"{trigger.secret_env} holding its secret is empty or unset"
                )
    return problems


__all__ = ["startup_problems", "trigger_secret"]
