"""Failure kinds derived from synthetic event traces and grades."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.agent_loop.events import EventRecord
from src.agent_loop.failures import (
    INFRA_KINDS,
    TaskOutcome,
    classify_scenario,
    classify_task,
    classify_tasks,
    outcome_from_result,
)
from src.llm import ModelProviderError

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _event(seconds: float, event_type: str, task_id: str = "t1", **payload: Any) -> EventRecord:
    return EventRecord(
        type=event_type,  # type: ignore[arg-type]
        source="test",
        payload=payload,
        timestamp=T0 + timedelta(seconds=seconds),
        task_id=task_id,
    )


def _task(terminal: str, task_id: str = "t1", *, denies: int = 0, **payload: Any) -> list[EventRecord]:
    events = [_event(0.0, "goal.started", task_id, task="Do it.")]
    events += [
        _event(1.0 + n, "permission.decided", task_id, decision="deny", tool="t") for n in range(denies)
    ]
    events.append(_event(1.0, "permission.decided", task_id, decision="allow", tool="t"))
    if terminal:
        events.append(_event(9.0, terminal, task_id, **payload))
    return events


def _outcome(kind: str, task_id: str = "t1") -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        status="completed",
        stop_reason="",
        error_type="",
        permission_denies=0,
        failure_kind=kind,  # type: ignore[arg-type]
    )


def test_a_provider_error_is_an_infra_failure() -> None:
    error = ModelProviderError("ollama 502", provider="ollama")

    outcome = classify_task(_task("goal.failed", status="failed", error=error))

    assert outcome.failure_kind == "provider_error"
    assert outcome.status == "failed"
    assert outcome.error_type == "ModelProviderError"


def test_a_failure_read_back_from_a_trace_file_keeps_its_error_type() -> None:
    error = {"type": "ModelProviderError", "message": "ollama 502"}

    assert classify_task(_task("goal.failed", status="failed", error=error)).failure_kind == "provider_error"


def test_a_timeout_is_an_infra_failure() -> None:
    error = TimeoutError("Goal runner timed out after 60 seconds.")

    assert classify_task(_task("goal.failed", status="failed", error=error)).failure_kind == "timeout"


def test_any_other_failure_is_an_error() -> None:
    error = RuntimeError("boom")

    outcome = classify_task(_task("goal.failed", status="failed", error=error))

    assert outcome.failure_kind == "error"
    assert outcome.error_type == "RuntimeError"


def test_a_task_without_a_terminal_event_is_an_error() -> None:
    outcome = classify_task(_task(""))

    assert outcome.failure_kind == "error"
    assert outcome.status == "missing"


@pytest.mark.parametrize("reason", ["approval_denied", "approval_hook_denied", "goal_hook_denied"])
def test_a_refused_approval_is_a_permission_failure(reason: str) -> None:
    outcome = classify_task(_task("goal.blocked", status="blocked", stop_reason=reason))

    assert outcome.failure_kind == "permission_denied"
    assert outcome.stop_reason == reason


def test_a_deny_that_ended_in_a_guard_limit_is_a_permission_failure() -> None:
    outcome = classify_task(
        _task("goal.blocked", denies=2, status="blocked", stop_reason="consecutive_failures")
    )

    assert outcome.failure_kind == "permission_denied"
    assert outcome.permission_denies == 2


def test_a_deny_in_a_completed_task_does_not_fail_it() -> None:
    outcome = classify_task(
        _task("goal.completed", denies=1, status="completed", stop_reason="answered")
    )

    assert outcome.failure_kind == "passed"
    assert outcome.permission_denies == 1


@pytest.mark.parametrize(
    "reason",
    ["turn_cap", "max_replans", "consecutive_failures", "no_plan_advance", "no_action", "invalid_decision"],
)
def test_a_loop_limit_is_a_guard_failure(reason: str) -> None:
    outcome = classify_task(_task("goal.blocked", status="blocked", stop_reason=reason))

    assert outcome.failure_kind == "guard_limit"


@pytest.mark.parametrize(
    ("terminal", "status", "payload"),
    [
        ("goal.blocked", "blocked", {"stop_reason": "model_stop"}),
        ("goal.cancelled", "cancelled", {"stop_reason": "model_stop"}),
        ("goal.blocked", "blocked", {}),
    ],
)
def test_a_model_that_gave_up_is_an_agent_stop(terminal: str, status: str, payload: dict[str, Any]) -> None:
    outcome = classify_task(_task(terminal, status=status, **payload))

    assert outcome.failure_kind == "agent_stopped"


def test_an_outside_cancellation_is_an_error() -> None:
    outcome = classify_task(_task("goal.cancelled", status="cancelled", stop_reason="cancelled"))

    assert outcome.failure_kind == "error"


def test_a_completed_task_passes() -> None:
    outcome = classify_task(_task("goal.completed", status="completed", stop_reason="answered"))

    assert outcome == TaskOutcome(
        task_id="t1",
        status="completed",
        stop_reason="answered",
        error_type="",
        permission_denies=0,
        failure_kind="passed",
    )


def test_tasks_are_classified_in_the_order_they_started() -> None:
    events = [
        *_task("goal.completed", "t1", status="completed"),
        *_task("goal.failed", "t2", status="failed", error=ModelProviderError("down", provider="ollama")),
        EventRecord(type="session.closed", source="test"),
    ]

    outcomes = classify_tasks(events)

    assert [(o.task_id, o.failure_kind) for o in outcomes] == [
        ("t1", "passed"),
        ("t2", "provider_error"),
    ]


def test_the_first_failed_task_decides_the_scenario() -> None:
    outcomes = [_outcome("passed", "t1"), _outcome("provider_error", "t2"), _outcome("guard_limit", "t3")]

    assert classify_scenario(outcomes, "failed") == "provider_error"


@pytest.mark.parametrize(
    ("grade", "kind"),
    [("passed", "passed"), ("failed", "grade_failed"), ("ungraded", "ungraded"), (None, "ungraded")],
)
def test_the_grade_decides_a_scenario_whose_tasks_all_passed(grade: str | None, kind: str) -> None:
    assert classify_scenario([_outcome("passed"), _outcome("passed", "t2")], grade) == kind


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [("ModelProviderError", "provider_error"), ("TimeoutError", "timeout"), ("OSError", "error"), ("", "error")],
)
def test_a_scenario_that_raised_before_any_task_uses_its_error_type(error_type: str, kind: str) -> None:
    assert classify_scenario([], None, error_type=error_type) == kind


def test_only_provider_errors_and_timeouts_are_infra_kinds() -> None:
    assert INFRA_KINDS == frozenset({"provider_error", "timeout"})


def test_a_scenario_that_raised_after_passing_tasks_uses_its_error_type() -> None:
    outcomes = [_outcome("passed", "t1")]

    assert classify_scenario(outcomes, "failed", error_type="ModelProviderError") == "provider_error"


@pytest.mark.parametrize(
    ("status", "reason", "goal_status", "kind"),
    [
        ("done", "answered", "completed", "passed"),
        ("blocked", "turn_cap", "blocked", "guard_limit"),
        ("blocked", "", "blocked", "agent_stopped"),
        ("cancelled", "model_stop", "cancelled", "agent_stopped"),
    ],
)
def test_a_task_result_without_a_trace_still_gets_a_kind(
    status: str, reason: str, goal_status: str, kind: str
) -> None:
    outcome = outcome_from_result("t1", status, reason)

    assert (outcome.status, outcome.stop_reason, outcome.failure_kind) == (goal_status, reason, kind)


def _judged_deny(task_id: str, error: str) -> list[EventRecord]:
    return [
        _event(2.0, "model.requested", task_id, phase="approval", tool="t"),
        _event(3.0, "model.responded", task_id, phase="approval", tool="t", needs_approval=True, error=error),
        _event(3.1, "permission.decided", task_id, decision="deny", tool="t", source="mode"),
    ]


@pytest.mark.parametrize(("error", "kind"), [("ModelProviderError", "provider_error"), ("TimeoutError", "timeout")])
def test_a_deny_caused_by_a_classifier_outage_is_an_infra_failure(error: str, kind: str) -> None:
    events = [
        _event(0.0, "goal.started", "t1", task="Do it."),
        *_judged_deny("t1", error),
        _event(9.0, "goal.blocked", "t1", status="blocked", stop_reason="model_stop"),
    ]

    outcome = classify_task(events)

    assert outcome.failure_kind == kind
    assert outcome.permission_denies == 1


def test_a_deny_after_an_unreadable_classifier_answer_stays_a_permission_failure() -> None:
    events = [
        _event(0.0, "goal.started", "t1", task="Do it."),
        *_judged_deny("t1", "no JSON answer"),
        _event(9.0, "goal.blocked", "t1", status="blocked", stop_reason="model_stop"),
    ]

    assert classify_task(events).failure_kind == "permission_denied"


def test_a_rule_deny_beside_a_classifier_outage_is_a_permission_failure() -> None:
    events = [
        _event(0.0, "goal.started", "t1", task="Do it."),
        *_judged_deny("t1", "ModelProviderError"),
        _event(4.0, "model.responded", "t1", phase="approval", tool="t", needs_approval=False, error=""),
        _event(4.1, "permission.decided", "t1", decision="deny", tool="t", source="rule"),
        _event(9.0, "goal.blocked", "t1", status="blocked", stop_reason="turn_cap"),
    ]

    assert classify_task(events).failure_kind == "permission_denied"
