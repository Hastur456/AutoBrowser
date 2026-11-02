"""Task usage accounting and run budgets (pure functions over :class:`TaskUsage`).

The engine adds every model call of a task — the plan, each agent turn, the approval
classifier, the step source's retargets — to ``LoopState.usage`` and checks the run budget
before each call it is about to make. A cost is known only while every call had reported
tokens and a price; one unknown call makes the task's cost ``None``, and a money budget is
then not checked (the call budget still is).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from src.config import LoopSettings
from src.contracts import RunBudget, TaskUsage

#: Bucket key of a model without a label.
UNKNOWN_MODEL = "unknown"


def effective_budget(profile: RunBudget, loop: LoopSettings) -> RunBudget:
    """The task's budget: each cap of the run profile, else the ``loop.max_task_*`` one."""

    return RunBudget(
        max_model_calls=(
            profile.max_model_calls
            if profile.max_model_calls is not None
            else loop.max_task_model_calls
        ),
        max_tokens=profile.max_tokens if profile.max_tokens is not None else loop.max_task_tokens,
        max_cost=profile.max_cost if profile.max_cost is not None else loop.max_task_cost,
    )


def add_call(
    usage: TaskUsage,
    *,
    stage: str,
    model: str = "",
    tokens: Mapping[str, int] | None = None,
    cost: float | None = None,
) -> TaskUsage:
    """``usage`` plus one model call of ``stage`` on ``model``."""

    tokens = dict(tokens or {})
    input_tokens = int(tokens.get("input_tokens") or 0)
    output_tokens = int(tokens.get("output_tokens") or 0)
    total_cost = None if usage.cost is None or cost is None else usage.cost + cost
    return replace(
        usage,
        model_calls=usage.model_calls + 1,
        input_tokens=usage.input_tokens + input_tokens,
        output_tokens=usage.output_tokens + output_tokens,
        cost=total_cost,
        by_stage=_add_to_bucket(usage.by_stage, stage, input_tokens, output_tokens, cost),
        by_model=_add_to_bucket(
            usage.by_model, model or UNKNOWN_MODEL, input_tokens, output_tokens, cost
        ),
    )


def _add_to_bucket(
    buckets: Mapping[str, Mapping[str, float]],
    key: str,
    input_tokens: int,
    output_tokens: int,
    cost: float | None,
) -> dict[str, dict[str, float]]:
    """A bucket keeps ``cost`` only while every call in it had a known cost."""

    updated = {name: dict(bucket) for name, bucket in buckets.items()}
    bucket = updated.get(key)
    if bucket is None:
        bucket = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        if cost is not None:
            bucket["cost"] = 0.0
    bucket["model_calls"] += 1
    bucket["input_tokens"] += input_tokens
    bucket["output_tokens"] += output_tokens
    if "cost" in bucket:
        if cost is None:
            del bucket["cost"]
        else:
            bucket["cost"] += cost
    updated[key] = bucket
    return updated


def budget_exhausted(usage: TaskUsage, budget: RunBudget) -> str:
    """Why one more model call would exceed ``budget``; ``""`` while there is room."""

    if budget.max_model_calls is not None and usage.model_calls >= budget.max_model_calls:
        return f"the task used its {budget.max_model_calls} model calls"
    if budget.max_tokens is not None and usage.tokens >= budget.max_tokens:
        return f"the task used {usage.tokens} of its {budget.max_tokens} tokens"
    if budget.max_cost is not None and usage.cost is not None and usage.cost >= budget.max_cost:
        return f"the task spent {usage.cost:.4f} of its {budget.max_cost:g} budget"
    return ""


__all__ = ["UNKNOWN_MODEL", "add_call", "budget_exhausted", "effective_budget"]
