"""``src/harness/otel_spans.py``: one task's events -> the JSON steps of its trajectory.

The same synthetic events as ``tests/test_otel_spans.py``; the content modes are the spans'.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agent_loop.events import EventRecord
from src.harness.otel_spans import (
    TRAJECTORY_KEY,
    SpanContext,
    task_span_specs,
    task_trajectory,
    trajectory_summary,
)
from tests.test_otel_spans import ANSWER, ARGS, CONTEXT, REASON, RESULT, TASK, ev, trace


def steps(events: list[EventRecord] | None = None, *, capture: str = "none") -> list[dict[str, Any]]:
    return task_trajectory(trace() if events is None else events, capture_content=capture)


def text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def proposal(ms: int, name: str = "click", args: dict[str, Any] | None = None, call_id: str = "call-1") -> EventRecord:
    request = {"name": name, "args": args or ARGS, "reason": "Press the button.", "id": call_id}
    return ev("action.proposed", ms, {"tool_request": request, "source": "model"})


def blocked_trace(gate: EventRecord) -> list[EventRecord]:
    return [
        ev("goal.started", 0, {"task": TASK}),
        proposal(10),
        gate,
        ev("goal.blocked", 30, {"status": "blocked", "stop_reason": "permission_denied", "turns": 1}),
    ]


def test_a_task_is_task_plan_tool_call_and_outcome_steps() -> None:
    result = steps()

    assert [step["type"] for step in result] == ["task", "plan", "tool_call", "outcome"]
    assert result[0] == {"type": "task", "task_id": "task-1"}
    assert result[1] == {"type": "plan", "replan_count": 0, "step_count": 1}
    assert result[2] == {"type": "tool_call", "tool": "click", "status": "success", "call_id": "call-1"}
    assert result[3] == {"type": "outcome", "status": "completed", "turns": 2}
    assert json.loads(text(result)) == result


@pytest.mark.parametrize("capture", ["none", "answers", "redacted"])
def test_content_modes(capture: str) -> None:
    result = steps(capture=capture)
    dumped = text(result)
    tool = result[2]

    if capture == "none":
        assert TASK not in dumped and ANSWER not in dumped
    else:
        assert result[0]["task"] == TASK and result[-1]["final_answer"] == ANSWER
    if capture == "redacted":
        assert tool["result"] == RESULT
        assert tool["arguments"] == {**ARGS, "api_key": "[REDACTED]"}
        assert result[1]["steps"] == [{"description": TASK, "status": "pending"}]
    else:
        assert RESULT not in dumped and "arguments" not in tool and "steps" not in result[1]
    assert "sk-live-1" not in dumped


def test_a_proposal_stopped_by_a_permission_deny_is_blocked_with_its_gate() -> None:
    gate = ev("permission.decided", 20, {"tool": "click", "server": "browser", "decision": "deny", "source": "rule", "rule_id": "shop", "reason": REASON})

    none, redacted = steps(blocked_trace(gate)), steps(blocked_trace(gate), capture="redacted")

    call = none[1]
    assert call["status"] == "blocked"
    assert call["gates"] == [{"gate": "permission.decided", "decision": "deny", "source": "rule", "rule_id": "shop", "tool": "click"}]
    assert REASON not in text(none)
    assert redacted[1]["gates"][0]["reason"] == REASON
    assert redacted[1]["reason"] == "Press the button."
    assert none[-1] == {"type": "outcome", "status": "blocked", "stop_reason": "permission_denied", "turns": 1}


def test_a_progress_block_is_a_blocked_call() -> None:
    gate = ev("policy.decided", 20, {"decision": "blocked", "source": "progress_guard", "rule_id": "repeat", "tool_request": {"name": "click"}})

    call = steps(blocked_trace(gate))[1]

    assert call["status"] == "blocked"
    assert call["gates"] == [{"gate": "policy.decided", "decision": "blocked", "source": "progress_guard", "rule_id": "repeat", "tool": "click"}]


def test_routine_allows_are_not_gates_and_a_call_without_a_result_did_not_run() -> None:
    allow = ev("permission.decided", 20, {"tool": "click", "decision": "allow", "source": "mode"})
    events = [ev("goal.started", 0, {"task": TASK}), proposal(10), allow, proposal(25, call_id="call-2"), ev("goal.completed", 30, {"final_answer": ANSWER})]

    result = steps(events)

    assert [step.get("status") for step in result[1:3]] == ["not_executed", "not_executed"]
    assert all("gates" not in step for step in result)


def test_the_arguments_that_ran_replace_the_proposed_ones() -> None:
    rewritten = {"element": "Add to basket", "ref": "e43"}
    events = [
        ev("goal.started", 0, {"task": TASK}),
        proposal(10),
        ev("tool.started", 20, {"tool_request": {"name": "click", "args": rewritten, "id": "call-1"}, "server": "browser"}),
        ev("tool.finished", 40, {"tool_result": {"name": "click", "status": "success", "content": RESULT}}),
        ev("goal.completed", 50, {}),
    ]

    call = steps(events, capture="redacted")[1]

    assert call["arguments"] == rewritten and call["server"] == "browser" and call["source"] == "model"
    assert len([step for step in steps(events) if step["type"] == "tool_call"]) == 1


def test_a_tool_error_keeps_its_message_only_when_redacted() -> None:
    events = [
        ev("goal.started", 0, {"task": TASK}),
        ev("tool.started", 10, {"tool_request": {"name": "click", "args": {}}}),
        ev("tool.finished", 20, {"tool_result": {"name": "click", "status": "error", "error": "ref e42 not found"}}),
        ev("goal.completed", 30, {}),
    ]

    assert steps(events)[1] == {"type": "tool_call", "tool": "click", "status": "error", "error": "tool_error"}
    assert steps(events, capture="redacted")[1]["error"] == "ref e42 not found"


def test_only_a_replan_adds_a_plan_step_and_model_errors_are_steps() -> None:
    plan = [{"id": 1, "description": "x", "status": "pending"}]
    events = [
        ev("goal.started", 0, {"task": TASK}),
        ev("plan.updated", 10, {"steps": plan, "current_step": 0, "replan_count": 0}),
        ev("plan.updated", 20, {"steps": [{**plan[0], "status": "done"}], "current_step": 1, "replan_count": 0}),
        ev("model.responded", 30, {"phase": "approval", "error": "timeout"}),
        ev("plan.updated", 40, {"steps": plan * 2, "current_step": 0, "replan_count": 1}),
        ev("goal.completed", 50, {}),
    ]

    result = steps(events)

    assert [(step["type"], step.get("replan_count")) for step in result] == [
        ("task", None), ("plan", 0), ("model_error", None), ("plan", 1), ("outcome", None)
    ]
    assert result[2] == {"type": "model_error", "phase": "approval"}
    assert steps(events, capture="redacted")[2]["error"] == "timeout"


def test_a_failed_goal_keeps_the_error_type_and_the_message_only_when_redacted() -> None:
    failed = ev("goal.failed", 270, {"error": {"type": "ModelProviderError", "message": "502 upstream"}})

    assert steps(trace(failed))[-1] == {"type": "outcome", "status": "failed", "error": {"type": "ModelProviderError"}}
    assert steps(trace(failed), capture="redacted")[-1]["error"]["message"] == "502 upstream"


def test_an_unfinished_task_ends_incomplete_and_no_start_gives_no_steps() -> None:
    events = trace()[:-1] + [proposal(300)]

    result = steps(events)

    assert result[-2]["status"] == "not_executed"
    assert result[-1] == {"type": "outcome", "status": "incomplete"}
    assert steps(trace()[1:]) == []


def test_the_summary_numbers_the_steps_and_takes_the_last_outcome() -> None:
    blocked = ev("goal.blocked", 270, {"stop_reason": "guard_limit", "turns": 3})
    first, second = steps(), task_trajectory(trace(blocked), capture_content="none")

    summary = trajectory_summary([*first, *second])

    assert [step["step"] for step in summary["full_trajectory"]] == list(range(1, len(first) + len(second) + 1))
    assert summary["stop_reason"] == "guard_limit" and summary["final_status"] == "blocked"
    assert trajectory_summary([]) == {"full_trajectory": [], "stop_reason": None, "final_status": None}


def test_the_task_span_carries_the_summary_only_when_asked() -> None:
    plain = task_span_specs(trace(), capture_content="none", context=CONTEXT)[0]
    root = task_span_specs(trace(), capture_content="none", context=CONTEXT, trajectory=True)[0]

    assert TRAJECTORY_KEY not in plain.attributes
    assert json.loads(str(root.attributes[TRAJECTORY_KEY])) == trajectory_summary(steps())
    aliased = SpanContext(attribute_aliases={TRAJECTORY_KEY: "backend.observation.metadata"})
    spans = task_span_specs(trace(), capture_content="none", context=aliased, trajectory=True)
    assert spans[0].attributes["backend.observation.metadata"] == root.attributes[TRAJECTORY_KEY]
    assert all(TRAJECTORY_KEY not in span.attributes for span in spans[1:])


def test_decisions_outside_a_call_are_gate_steps_where_they_happened() -> None:
    goal_hook = ev("hook.decided", 5, {"hook_id": "h-start", "event": "goal_start", "decision": "deny", "reason": REASON})
    stop_hook = ev("hook.decided", 15, {"hook_id": "h-stop", "event": "stop", "decision": "deny"})
    events = [ev("goal.started", 0, {"task": TASK}), goal_hook, stop_hook, proposal(20), ev("goal.cancelled", 30, {})]

    result = steps(events)

    assert result[1] == {"type": "gate", "gate": "hook.decided", "hook_id": "h-start", "event": "goal_start", "decision": "deny"}
    assert result[2]["event"] == "stop" and result[2]["type"] == "gate"
    assert result[3] == {"type": "tool_call", "tool": "click", "status": "not_executed", "call_id": "call-1", "source": "model"}
    assert steps(events, capture="redacted")[1]["reason"] == REASON


def test_a_hook_gate_keeps_its_hook_event_and_an_ask_alone_does_not_block() -> None:
    ask = ev("hook.decided", 20, {"hook_id": "h1", "event": "pre_tool_use", "decision": "ask", "tool": "click"})
    events = [ev("goal.started", 0, {"task": TASK}), proposal(10), ask, ev("goal.cancelled", 30, {})]

    call = steps(events)[1]

    assert call["gates"] == [{"gate": "hook.decided", "hook_id": "h1", "event": "pre_tool_use", "decision": "ask", "tool": "click"}]
    assert call["status"] == "not_executed"
