"""Tests for the history shaping in :mod:`src.harness.memory` (L1 budget, L3 task digest).

Every test pins an explicit :class:`~src.config.MemorySettings`; none reads ``get_settings()``.
"""

from __future__ import annotations

import random

import pytest

from src.config import MemorySettings
from src.harness.memory import (
    CLEARED_TOOL_OUTPUT_PREFIX,
    COMPACTED_TOOL_OUTPUT_PREFIX,
    TASK_DIGEST_PREFIX,
    MemoryManager,
    history_chars,
)
from src.messages import (
    Message,
    ToolCall,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)


def _manager(**fields: object) -> MemoryManager:
    return MemoryManager(settings=MemorySettings(**fields))


def _call(call_id: str, name: str, **arguments: object) -> Message:
    return assistant_message(tool_calls=(ToolCall(id=call_id, name=name, arguments=dict(arguments)),))


def _result(call_id: str, name: str, content: str) -> Message:
    return tool_message(tool_call_id=call_id, content=content, name=name)


def _task(task_id: str, request: str, steps: list[tuple[str, str]], answer: str | None) -> list[Message]:
    messages = [user_message(f"User request ({task_id}):\n{request}")]
    for index, (name, content) in enumerate(steps):
        call_id = f"{task_id}-{index}"
        messages.append(_call(call_id, name, step=index))
        messages.append(_result(call_id, name, content))
    if answer is not None:
        messages.append(assistant_message(content=answer))
    return messages


def _assert_pairs_valid(messages: list[Message]) -> None:
    """Every tool result answers an earlier call, every call has its result."""

    open_calls: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            open_calls.add(call.id)
        if message.role == "tool":
            assert message.tool_call_id in open_calls, message
            answered.add(str(message.tool_call_id))
    assert answered == open_calls


# --------------------------------------------------------------------------
# apply_history_budget
# --------------------------------------------------------------------------


def _long_history() -> list[Message]:
    return [
        system_message("system"),
        *_task(
            "task-1",
            "find a kettle",
            [("tool_a", "a" * 2000), ("tool_b", "b" * 2000), ("tool_c", "c" * 2000), ("tool_d", "d" * 2000)],
            answer=None,
        ),
    ]


def test_a_zero_budget_is_a_no_op() -> None:
    history = _long_history()

    assert _manager(history_budget_chars=0).apply_history_budget(history) == history


def test_a_history_within_budget_is_unchanged() -> None:
    history = _long_history()

    manager = _manager(history_budget_chars=history_chars(history))

    assert manager.apply_history_budget(history) == history


def test_the_oldest_tool_outputs_are_cleared_first() -> None:
    history = _long_history()

    shaped = _manager(history_budget_chars=5000, keep_recent_tool_results=1).apply_history_budget(history)

    tools = [message for message in shaped if message.role == "tool"]
    assert tools[0].content.startswith(f"{CLEARED_TOOL_OUTPUT_PREFIX} tool_a output")
    assert "(2000 chars)" in tools[0].content
    assert tools[1].content.startswith(CLEARED_TOOL_OUTPUT_PREFIX)
    # Clearing stops as soon as the history fits.
    assert tools[2].content == "c" * 2000
    assert history_chars(shaped) <= 5000


def test_the_newest_tool_outputs_and_non_tool_messages_are_never_cleared() -> None:
    history = _long_history()

    shaped = _manager(history_budget_chars=10, keep_recent_tool_results=2).apply_history_budget(history)

    tools = [message for message in shaped if message.role == "tool"]
    assert [message.content[:1] for message in tools[2:]] == ["c", "d"]
    assert [message for message in shaped if message.role != "tool"] == [
        message for message in history if message.role != "tool"
    ]


def test_cleared_outputs_keep_their_tool_call_pairing() -> None:
    history = _long_history()

    shaped = _manager(history_budget_chars=10, keep_recent_tool_results=0).apply_history_budget(history)

    assert all(message.content.startswith(CLEARED_TOOL_OUTPUT_PREFIX) for message in shaped if message.role == "tool")
    assert [message.tool_call_id for message in shaped] == [message.tool_call_id for message in history]
    assert [message.name for message in shaped] == [message.name for message in history]
    _assert_pairs_valid(shaped)


def test_compacted_outputs_and_short_outputs_are_left_alone() -> None:
    compacted = f"{COMPACTED_TOOL_OUTPUT_PREFIX} tool_a output from an earlier step (5000 chars)."
    history = [
        _call("1", "tool_a"),
        _result("1", "tool_a", compacted),
        _call("2", "tool_b"),
        _result("2", "tool_b", "ok"),
        _call("3", "tool_c"),
        _result("3", "tool_c", "x" * 3000),
    ]

    shaped = _manager(history_budget_chars=10, keep_recent_tool_results=0).apply_history_budget(history)

    assert shaped[1].content == compacted
    assert shaped[3].content == "ok"
    assert shaped[5].content.startswith(CLEARED_TOOL_OUTPUT_PREFIX)


def test_the_budget_is_idempotent() -> None:
    manager = _manager(history_budget_chars=5000, keep_recent_tool_results=1)

    once = manager.apply_history_budget(_long_history())

    assert manager.apply_history_budget(once) == once


def test_ensure_history_applies_the_budget_after_compaction() -> None:
    manager = _manager(history_budget_chars=3000, keep_recent_tool_results=1)
    state = {"messages": _long_history()[1:], "task": "find a kettle", "task_id": "task-1"}

    shaped = manager.ensure_history(state, system_prompt="system")

    assert shaped[0].role == "system"
    assert history_chars(shaped) <= 3000 + len("system") + 200
    assert shaped[-1].content == "d" * 2000


# --------------------------------------------------------------------------
# digest_tasks
# --------------------------------------------------------------------------


def _session_history() -> list[Message]:
    return [
        system_message("system"),
        *_task("task-1", "find a kettle", [("browser_navigate", "page 1"), ("browser_click", "page 2")], "Kettle: 1990 ₽"),
        *_task("task-2", "open the first item", [("browser_click", "item")], "Opened it."),
        *_task("task-3", "add it to cart", [("browser_click", "cart"), ("browser_click", "cart 2")], None),
    ]


def test_keep_zero_is_a_no_op() -> None:
    history = _session_history()

    assert _manager(keep_recent_tasks=0).digest_tasks(history) == history


def test_no_digest_when_there_are_not_more_tasks_than_kept() -> None:
    history = _session_history()

    assert _manager(keep_recent_tasks=3).digest_tasks(history) == history


def test_old_tasks_become_one_digest_each_in_order() -> None:
    history = _session_history()

    shaped = _manager(keep_recent_tasks=1).digest_tasks(history)

    assert shaped[0] == history[0]
    assert shaped[1].role == "user"
    assert shaped[1].content == "\n".join(
        [
            TASK_DIGEST_PREFIX,
            "- request: find a kettle",
            "- answer: Kettle: 1990 ₽",
            "- tools used: browser_navigate×1, browser_click×1",
        ]
    )
    assert shaped[2].content.startswith(TASK_DIGEST_PREFIX)
    assert "- request: open the first item" in shaped[2].content
    # The newest task stays verbatim.
    assert shaped[3:] == _task(
        "task-3", "add it to cart", [("browser_click", "cart"), ("browser_click", "cart 2")], None
    )
    _assert_pairs_valid(shaped)


def test_a_task_without_a_final_answer_is_marked() -> None:
    history = _session_history()

    shaped = _manager(keep_recent_tasks=1).digest_tasks([*history, *_task("task-4", "next", [], "x")])

    third = shaped[3]
    assert "- answer: (no final answer)" in third.content
    assert "- tools used: browser_click×2" in third.content


def test_long_requests_and_answers_are_truncated() -> None:
    history = [
        *_task("task-1", "r" * 1000, [], "a" * 2000),
        *_task("task-2", "next", [], "done"),
    ]

    digest = _manager(keep_recent_tasks=1).digest_tasks(history)[0].content

    request_line, answer_line = digest.splitlines()[1:3]
    assert len(request_line) <= len("- request: ") + 300
    assert len(answer_line) <= len("- answer: ") + 500
    assert request_line.endswith("[truncated]")


def test_existing_digests_are_kept_on_the_next_boundary() -> None:
    manager = _manager(keep_recent_tasks=1)
    first = manager.digest_tasks(_session_history())

    second = manager.digest_tasks([*first, *_task("task-4", "checkout", [], "ok")])

    digests = [message for message in second if message.content.startswith(TASK_DIGEST_PREFIX)]
    assert len(digests) == 3
    assert second[-2].content.startswith("User request (task-4)")


# --------------------------------------------------------------------------
# Property: pairs stay valid under any mix of compaction, budget and digest
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(25))
def test_pairs_stay_valid_after_compaction_budget_and_digest(seed: int) -> None:
    rng = random.Random(seed)
    names = ["tool_a", "tool_b", "tool_c"]
    history: list[Message] = [system_message("system")]
    for task in range(rng.randint(1, 5)):
        steps = [
            (rng.choice(names), "x" * rng.randint(0, 4000))
            for _ in range(rng.randint(0, 6))
        ]
        answer = rng.choice([None, "answer"])
        history.extend(_task(f"task-{task}", f"request {task}", steps, answer))

    manager = _manager(
        compact_tool_output_min_chars=rng.randint(0, 2000),
        history_budget_chars=rng.randint(0, 20000),
        keep_recent_tool_results=rng.randint(0, 3),
        keep_recent_tasks=rng.randint(0, 3),
    )

    shaped = manager.digest_tasks(history)
    shaped = manager.apply_history_budget(manager.compact_snapshot_history(shaped))

    _assert_pairs_valid(shaped)
    assert shaped[0].role == "system"
    assert manager.apply_history_budget(shaped) == shaped
