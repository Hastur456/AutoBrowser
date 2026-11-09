"""What stops the stand from starting, checked before any browser runs.

``ServeSettings`` already validated its own fields; these checks need the rest of the
configuration (permissions, replay, prices) or the environment. Problems never quote a secret.
"""

from __future__ import annotations

import json
import os

from src.config import PROFILE_DIR_PLACEHOLDER, Settings, TriggerSpec


def trigger_secret(trigger: TriggerSpec) -> str:
    """The shared secret of a webhook trigger (inline, or from its environment variable)."""

    if trigger.secret is not None:
        return trigger.secret.get_secret_value()
    if trigger.secret_env:
        return os.environ.get(trigger.secret_env, "")
    return ""


def priced_models(settings: Settings) -> list[str]:
    """The model of every tier a run may call: the session model, then the cascade's."""

    models = [settings.llm.model]
    if settings.cascade.enabled:
        models += [tier.model for tier in settings.cascade.tiers if tier.model not in models]
    return models


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
    money = (
        serve.daily_max_cost is not None
        or serve.default_run_budget.max_cost is not None
        or settings.loop.max_task_cost is not None
        or any(job.budget is not None and job.budget.max_cost is not None for job in serve.jobs.values())
    )
    if money:
        for model in priced_models(settings):
            if model not in settings.llm.prices:
                problems.append(
                    f"a money cap is set but llm.prices has no price for the model {model!r}"
                )
    if serve.workers > 1 and settings.memory.persistent_enabled:
        problems.append(
            "serve.workers > 1 needs memory.persistent_enabled=false: the workers would share "
            "the memory files (file locking comes later)"
        )
    for name, job in serve.jobs.items():
        for trigger in job.triggers:
            if trigger.type == "webhook" and trigger.secret_env and not trigger_secret(trigger):
                problems.append(
                    f"job {name!r}, trigger {trigger.id!r}: the environment variable "
                    f"{trigger.secret_env} holding its secret is empty or unset"
                )
    return problems


def startup_warnings(settings: Settings) -> list[str]:
    """What the stand starts with but probably should not; printed, never a refusal."""

    serve = settings.serve
    warnings: list[str] = []
    if serve.workers > 1 and settings.browser.launch == "external":
        # Name-free: no server is singled out; one that names the placeholder isolates them.
        named = any(
            PROFILE_DIR_PLACEHOLDER in json.dumps(config.model_dump(mode="json"))
            for config in settings.mcp_servers.values()
        )
        if not named:
            warnings.append(
                f"{serve.workers} workers with browser.launch external, but no mcp_servers entry "
                f"names {PROFILE_DIR_PLACEHOLDER}: the workers' browsers may share one profile "
                f"(use --user-data-dir {PROFILE_DIR_PLACEHOLDER}, or let the server isolate them)"
            )
    cascade = settings.cascade
    if (
        cascade.enabled
        and cascade.tiers
        # Equal still escalates: the tier check runs before the failure terminal.
        and cascade.escalate_after_failures > settings.loop.max_consecutive_failures
    ):
        warnings.append(
            f"cascade.escalate_after_failures {cascade.escalate_after_failures} is above "
            f"loop.max_consecutive_failures {settings.loop.max_consecutive_failures}: failures "
            f"end a task before its tier goes up"
        )
    return warnings


__all__ = ["priced_models", "startup_problems", "startup_warnings", "trigger_secret"]
