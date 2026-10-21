from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agent_loop.events import EventRecord
from src.ui.feed import project_event, sanitize_text

BIDI_OVERRIDE = chr(0x202E)
BIDI_ISOLATE = chr(0x2066)


def record(event_type: str, payload: dict[str, object], **fields: object) -> dict[str, object]:
    return EventRecord(
        type=event_type,  # type: ignore[arg-type]
        source="test",
        payload=payload,
        timestamp=datetime(2026, 10, 19, tzinfo=UTC),
        session_id="s1",
        task_id=str(fields.get("task_id", "t1")),
        goal_id="t1",
        sequence=int(fields.get("sequence", 1)),  # type: ignore[arg-type]
    ).to_json_dict()


def test_sanitize_text_drops_escape_sequences_and_controls() -> None:
    hostile = (
        "a\x1b[31mred\x1b[0m"
        "\x1b]52;c;ZXZpbA==\x07"  # OSC 52: write to the clipboard
        "\x1b]0;title\x1b\\"  # OSC 0 with an ST terminator
        "b\r\nc\rd\x00\x07\x9b"
        f"{BIDI_OVERRIDE}e{BIDI_ISOLATE}\tf"
    )

    assert sanitize_text(hostile) == "aredb\ncde\tf"
    assert sanitize_text(None) == ""
    assert sanitize_text("Кириллица, 中文, emoji 🙂") == "Кириллица, 中文, emoji 🙂"


def test_call_and_result_items_carry_names_as_data_and_page_url() -> None:
    call = project_event(
        record(
            "action.proposed",
            {
                "tool_request": {
                    "name": "any_tool",
                    "args": {"url": "https://x.test", "password": "hunter2", "q": "\x1b[2Jhi"},
                    "reason": "why",
                }
            },
        )
    )
    finished = project_event(
        record(
            "tool.finished",
            {
                "tool_result": {
                    "name": "any_tool",
                    "status": "success",
                    "content": "- Page URL: https://x.test/a\n- Page Title: X",
                }
            },
            sequence=2,
        )
    )

    assert call is not None and finished is not None
    assert (call.kind, call.tool, call.text) == ("call", "any_tool", "why")
    assert call.args == {"url": "https://x.test", "password": "[REDACTED]", "q": "hi"}
    assert (finished.kind, finished.status, finished.url) == (
        "tool_finished",
        "success",
        "https://x.test/a",
    )
    assert finished.sequence == 2 and finished.task_id == "t1"
    assert finished.timestamp == datetime(2026, 10, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    "event_type, payload, expected",
    [
        ("permission.decided", {"decision": "allow", "tool": "t"}, ("allow", "permission")),
        ("permission.decided", {"decision": "deny", "rule_id": "r1"}, ("deny", "permission")),
        ("hook.decided", {"decision": "deny", "hook_id": "urls"}, ("deny", "hook")),
        ("hook.decided", {"decision": None, "error": "timeout"}, ("error", "hook")),
        ("policy.decided", {"decision": "blocked", "reason": "repeat"}, ("blocked", "guard")),
    ],
)
def test_gate_items(event_type: str, payload: dict[str, object], expected: tuple[str, str]) -> None:
    item = project_event(record(event_type, payload))

    assert item is not None
    assert (item.kind, item.decision, item.origin) == ("gate", *expected)


@pytest.mark.parametrize(
    "event_type, payload",
    [
        ("hook.decided", {"decision": "allow", "hook_id": "audit"}),
        ("policy.decided", {"decision": "approved"}),
        ("model.responded", {"phase": "agent"}),
        ("session.started", {}),
        ("task.started", {}),
    ],
)
def test_events_a_feed_ignores(event_type: str, payload: dict[str, object]) -> None:
    assert project_event(record(event_type, payload)) is None


def test_outcome_reads_structured_and_legacy_goal_payloads() -> None:
    structured = project_event(
        record(
            "goal.completed",
            {"task": "t", "status": "completed", "final_answer": "Done.", "turns": 3},
        )
    )
    legacy = project_event(record("goal.completed", {"task": "t", "result": {"final_answer": "Old."}}))
    failed = project_event(
        record("goal.failed", {"task": "t", "status": "failed", "error": TimeoutError("slow")})
    )
    cancelled = project_event(record("goal.cancelled", {"task": "t", "reason": "stopped"}))

    assert structured is not None and (structured.status, structured.text, structured.turns) == (
        "completed",
        "Done.",
        3,
    )
    assert legacy is not None and legacy.text == "Old."
    assert failed is not None and (failed.status, failed.text) == ("failed", "TimeoutError: slow")
    assert cancelled is not None and (cancelled.status, cancelled.text) == ("cancelled", "stopped")


def test_plan_and_turn_items() -> None:
    plan = project_event(
        record(
            "plan.updated",
            {"steps": ["Open", {"description": "Search"}, "", "\x1b[1mPick"], "replan_count": 1},
        )
    )
    turn = project_event(record("turn.started", {"turn": 4, "current_step": 2}))

    assert plan is not None and plan.steps == ("Open", "Search", "Pick")
    assert plan.step_statuses == ("", "", "")
    assert plan.replan_count == 1
    engine_plan = project_event(
        record(
            "plan.updated",
            {
                "steps": [
                    {"id": 1, "description": "Open", "status": "completed"},
                    {"id": 2, "description": "Search", "status": "in_progress"},
                ],
                "current_step": 1,
                "replan_count": 0,
            },
        )
    )
    assert engine_plan is not None
    assert engine_plan.step_statuses == ("completed", "in_progress")
    assert engine_plan.current_step == 1
    assert turn is not None and (turn.turn, turn.current_step) == (4, 2)


def test_unknown_and_memory_events() -> None:
    other = project_event(record("custom.thing", {}))
    memory = project_event(record("memory.skipped", {"path": "sites/x.md", "reason": "policy"}))

    assert other is not None and other.kind == "other" and other.event_type == "custom.thing"
    assert memory is not None and (memory.kind, memory.status, memory.text) == (
        "memory",
        "skipped",
        "policy",
    )
