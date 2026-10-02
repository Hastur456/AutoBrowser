"""Progress guard: identical-outcome repeats and the shared non-terminal block path."""

from __future__ import annotations

from src.agent_loop.execution.guards import progress_block_reason, tool_block_updates
from src.agent_loop.execution.progress import (
    ActionRecord,
    ineffective_repeat_reason,
    record_action,
)
from src.agent_loop.execution.state import LoopState


def journal(*calls: tuple[str, dict, str]) -> list[ActionRecord]:
    history: list[ActionRecord] = []
    for tool, args, content in calls:
        history.append(
            record_action(
                history,
                {"name": tool, "args": args},
                {"name": tool, "status": "success", "content": content, "error": ""},
                preview_chars=80,
            )
        )
    return history


def test_no_reason_below_the_limit() -> None:
    history = journal(("echo", {"text": "a"}, "a"), ("echo", {"text": "a"}, "a"))
    assert ineffective_repeat_reason(history, "echo", {"text": "a"}, 3) is None
    assert ineffective_repeat_reason([], "echo", {"text": "a"}, 1) is None


def test_reason_once_the_identical_outcome_reaches_the_limit() -> None:
    history = journal(*[("echo", {"text": "a"}, "a")] * 3)
    reason = ineffective_repeat_reason(history, "echo", {"text": "a"}, 3)
    assert reason is not None
    assert reason.startswith("Not executed: echo with these exact arguments")
    assert "3 times" in reason


def test_other_arguments_and_interleaved_calls_are_tracked_per_call() -> None:
    history = journal(
        ("echo", {"text": "a"}, "a"),
        ("echo", {"text": "b"}, "b"),
        ("echo", {"text": "a"}, "a"),
        ("echo", {"text": "b"}, "b"),
    )
    assert ineffective_repeat_reason(history, "echo", {"text": "a"}, 2) is not None
    assert ineffective_repeat_reason(history, "echo", {"text": "c"}, 2) is None


def test_a_changed_outcome_resets_the_count() -> None:
    history = journal(
        ("echo", {"text": "a"}, "a"),
        ("echo", {"text": "a"}, "a"),
        ("echo", {"text": "a"}, "different"),
    )
    assert ineffective_repeat_reason(history, "echo", {"text": "a"}, 2) is None


def test_progress_guard_blocks_an_empty_request() -> None:
    assert progress_block_reason(LoopState(), None) == "No tool request was provided."
    assert progress_block_reason(LoopState(), {"name": "", "args": {}}) == (
        "No tool request was provided."
    )
    assert progress_block_reason(LoopState(), {"name": "echo", "args": {}}) is None


def test_tool_block_updates_is_a_non_terminal_tool_message() -> None:
    request = {"name": "echo", "args": {"text": "a"}, "id": "call_1"}
    state = LoopState().apply({"tool_request": request, "consecutive_failures": 1})
    updates = tool_block_updates(state, "nope", event={"source": "rule", "rule_id": "r1"})
    state = state.apply(updates)
    assert state.policy_decision == "blocked"
    assert state.consecutive_failures == 2
    assert state.error == state.observation == "nope"
    assert state.policy_event == {
        "decision": "blocked",
        "reason": "nope",
        "tool_request": request,
        "source": "rule",
        "rule_id": "r1",
    }
    assert "decision" not in updates  # the loop keeps going
    assert state.messages[-1].content == "echo\n\nnope"
