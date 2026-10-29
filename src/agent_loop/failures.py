"""Failure kinds of batch runs, derived from traces and grades (pure, no I/O).

One ``FailureKind`` per task and per scenario separates an infrastructure outage (the model
provider or the time budget) from a failure of the agent, so batch analytics never calls a
provider outage an agent regression. Kinds come from goal events, the ``stop_reason`` they
carry, permission decisions and the grade — never from site or page knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.agent_loop.benchmarks import split_by_task
from src.agent_loop.events import EventRecord

FailureKind = Literal[
    "passed",
    "provider_error",
    "timeout",
    "permission_denied",
    "guard_limit",
    "agent_stopped",
    "grade_failed",
    "error",
    "ungraded",
]

#: Kinds that are not the agent's fault: rerun instead of debugging the agent.
INFRA_KINDS: frozenset[str] = frozenset({"provider_error", "timeout"})

_TERMINAL_EVENTS = {"goal.completed", "goal.failed", "goal.blocked", "goal.cancelled"}
_PROVIDER_ERRORS = {"ModelProviderError"}
_TIMEOUT_ERRORS = {"TimeoutError", "asyncio.TimeoutError"}
_APPROVAL_REASONS = {"approval_denied", "approval_hook_denied", "goal_hook_denied"}
_GUARD_REASONS = {
    "turn_cap",
    "max_replans",
    "consecutive_failures",
    "no_plan_advance",
    "no_action",
    "invalid_decision",
}


@dataclass(frozen=True)
class TaskOutcome:
    """How one task ended, with the raw facts its ``failure_kind`` was derived from."""

    task_id: str
    #: Goal status: ``completed``/``blocked``/``failed``/``cancelled``, or ``missing``.
    status: str
    #: ``stop_reason`` of the terminal goal event, ``""`` when absent.
    stop_reason: str
    #: ``error.type`` of ``goal.failed``, ``""`` otherwise.
    error_type: str
    #: ``permission.decided`` events with decision ``deny`` in this task.
    permission_denies: int
    failure_kind: FailureKind


def error_kind(error_type: str) -> FailureKind:
    """Kind of a failure that raised: provider error, timeout or a plain error."""

    if error_type in _PROVIDER_ERRORS:
        return "provider_error"
    if error_type in _TIMEOUT_ERRORS:
        return "timeout"
    return "error"


def classify_task(events: Sequence[EventRecord]) -> TaskOutcome:
    """Classify one task's events; the first matching rule wins."""

    task_id = next((str(event.task_id) for event in events if event.task_id), "")
    terminal = next((event for event in events if event.type in _TERMINAL_EVENTS), None)
    denies = sum(
        1
        for event in events
        if event.type == "permission.decided" and event.payload.get("decision") == "deny"
    )
    if terminal is None:
        return TaskOutcome(task_id, "missing", "", "", denies, "error")

    payload = terminal.payload
    stop_reason = str(payload.get("stop_reason") or "")
    if terminal.type == "goal.failed":
        error_type = _error_type(payload.get("error"))
        return TaskOutcome(task_id, "failed", stop_reason, error_type, denies, error_kind(error_type))

    status = str(payload.get("status") or terminal.type.removeprefix("goal."))
    return TaskOutcome(
        task_id, status, stop_reason, "", denies, _ended_kind(status, stop_reason, denies)
    )


def classify_tasks(events: Iterable[EventRecord]) -> list[TaskOutcome]:
    """Classify every task of a session trace, in the order the tasks started."""

    return [classify_task(task_events) for task_events in split_by_task(events)]


def classify_scenario(
    outcomes: Sequence[TaskOutcome],
    grade_status: str | None,
    *,
    error_type: str = "",
) -> FailureKind:
    """Kind of a scenario: its first failed task, else its grade.

    A scenario that raised before any task ended (no outcomes) is classified by the
    ``error_type`` of the exception the batch recorded.
    """

    if not outcomes:
        return error_kind(error_type)
    for outcome in outcomes:
        if outcome.failure_kind != "passed":
            return outcome.failure_kind
    if grade_status == "failed":
        return "grade_failed"
    if grade_status == "passed":
        return "passed"
    return "ungraded"


def _ended_kind(status: str, stop_reason: str, denies: int) -> FailureKind:
    if stop_reason in _APPROVAL_REASONS:
        return "permission_denied"
    if status == "completed":
        return "passed"
    if denies > 0:
        # A deny made the task fail even when the final reason is a guard or a model stop.
        return "permission_denied"
    if stop_reason in _GUARD_REASONS:
        return "guard_limit"
    if stop_reason in {"", "model_stop"}:
        # The model gave up; why is in its answer, deliberately not parsed.
        return "agent_stopped"
    # An outside cancellation: neither the model's choice nor a pass.
    return "error"


def _error_type(error: Any) -> str:
    if isinstance(error, BaseException):
        return type(error).__name__
    if isinstance(error, dict):
        return str(error.get("type") or "")
    return ""


__all__ = [
    "INFRA_KINDS",
    "FailureKind",
    "TaskOutcome",
    "classify_scenario",
    "classify_task",
    "classify_tasks",
    "error_kind",
]
