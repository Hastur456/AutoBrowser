"""A finished batch folder in the Part A format, for the platform bridge tests.

``make_batch`` writes ``batch.json``, the ``tasks.jsonl`` copy, ``run_index.jsonl`` and one
``traces/<scenario>.events.jsonl`` per traced scenario, built from synthetic events with
explicit timestamps. No session runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.agent_loop.batch import tasks_sha256
from src.agent_loop.events import EventRecord

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
ANSWER_TEXT = "Цена книги: £54.23 (секретный ответ)"
TOOL_RESULT_TEXT = "page text: In stock (22 available)"


@dataclass
class Scenario:
    scenario_id: str
    failure_kind: str = "passed"
    tasks: list[str] = field(default_factory=lambda: ["find the price"])
    expected: dict[str, Any] | None = field(default_factory=lambda: {"contains": "£"})
    grade: str | None = "passed"
    traced: bool = True
    tool_calls: int = 1
    error: dict[str, str] | None = None
    reference: str | None = None


def task_events(session_id: str, task_id: str, task: str, start: datetime, *, tool_calls: int, answer: str) -> list[EventRecord]:
    def ev(kind: str, ms: int, payload: dict[str, Any]) -> EventRecord:
        return EventRecord(
            type=kind,  # type: ignore[arg-type]
            source="test",
            payload=payload,
            event_id=f"{task_id}-{kind}-{ms}",
            timestamp=start + timedelta(milliseconds=ms),
            session_id=session_id,
            goal_id=task_id,
            task_id=task_id,
        )

    events = [
        ev("goal.started", 0, {"task": task}),
        ev("model.requested", 10, {"phase": "plan"}),
        ev("model.responded", 60, {"phase": "plan", "usage": {"input_tokens": 1000, "output_tokens": 20}}),
    ]
    at = 70
    for index in range(tool_calls):
        events += [
            ev("model.requested", at, {"phase": "agent"}),
            ev("model.responded", at + 30, {"phase": "agent", "usage": {"input_tokens": 2000, "output_tokens": 30}}),
            ev("permission.decided", at + 31, {"tool": "click", "server": "browser", "decision": "allow", "source": "mode", "rule_id": "", "reason": "r"}),
            ev("tool.started", at + 32, {"tool_request": {"name": "click", "args": {"element": f"button {index}"}, "id": f"call-{index}"}}),
            ev("tool.finished", at + 82, {"tool_result": {"name": "click", "status": "success", "content": TOOL_RESULT_TEXT, "error": ""}}),
        ]
        at += 100
    events.append(ev("goal.completed", at, {"task": task, "status": "completed", "final_answer": answer, "turns": tool_calls + 1, "stop_reason": "answered"}))
    return events


def make_batch(
    root: Path,
    scenarios: list[Scenario],
    *,
    batch_id: str = "batch-1a2b3c4d5e6f",
    started: datetime = T0,
    graded: bool = True,
    model: str = "gemma4:31b-cloud",
    tasks_hash: str | None = None,
    finished: bool = True,
) -> Path:
    batch_dir = root / batch_id
    (batch_dir / "traces").mkdir(parents=True)
    tasks_lines = []
    for scenario in scenarios:
        line: dict[str, Any] = {"scenario_id": scenario.scenario_id, "tasks": scenario.tasks}
        if scenario.expected is not None:
            line["expected"] = scenario.expected
        if scenario.reference is not None:
            line["reference"] = scenario.reference
        tasks_lines.append(json.dumps(line, ensure_ascii=False))
    (batch_dir / "tasks.jsonl").write_text("\n".join(tasks_lines) + "\n", encoding="utf-8")

    rows = []
    for index, scenario in enumerate(scenarios):
        session_id = f"session-{scenario.scenario_id}"
        begin = started + timedelta(minutes=index)
        task_outcomes = []
        trace_path = None
        if scenario.traced:
            labels = {
                "autobrowser.batch.id": batch_id,
                "autobrowser.scenario.id": scenario.scenario_id,
                "deployment.environment.name": "batch",
                "vcs.ref.head.revision": "c" * 40,
            }
            events = [
                EventRecord(
                    type="session.started",
                    source="test",
                    payload={"session_id": session_id, "resumed": False, "labels": labels},
                    event_id=f"{session_id}-started",
                    timestamp=begin,
                    session_id=session_id,
                )
            ]
            for number, task in enumerate(scenario.tasks):
                task_id = f"{scenario.scenario_id}-task-{number}"
                events += task_events(session_id, task_id, task, begin + timedelta(seconds=1 + number * 10), tool_calls=scenario.tool_calls, answer=ANSWER_TEXT)
                task_outcomes.append({"task_id": task_id, "status": "completed", "stop_reason": "answered", "error_type": "", "permission_denies": 0, "failure_kind": "passed"})
            trace_path = f"traces/{scenario.scenario_id}.events.jsonl"
            (batch_dir / trace_path).write_text(
                "\n".join(json.dumps(event.to_json_dict(), ensure_ascii=False) for event in events) + "\n", encoding="utf-8"
            )
        row: dict[str, Any] = {
            "batch_id": batch_id,
            "scenario_id": scenario.scenario_id,
            "session_id": session_id if scenario.traced else None,
            "status": "failed" if scenario.error else "completed",
            "tasks": scenario.tasks,
            "expected": scenario.expected,
            "tags": [],
            "started_at": begin.isoformat(),
            "finished_at": (begin + timedelta(seconds=30)).isoformat(),
            "error": scenario.error,
            "task_statuses": ["done"] * len(task_outcomes),
            "final_answer": None if scenario.error else ANSWER_TEXT,
            "metrics": {"duration_ms": 30_000, "model_calls": 2, "tool_calls": scenario.tool_calls, "tool_errors": 0, "policy_blocks": 0, "time_to_first_tool_ms": 102},
            "schema_version": 1,
            "task_outcomes": task_outcomes,
            "failure_kind": scenario.failure_kind,
            "trace_path": trace_path,
            "trace_text_path": None,
        }
        if graded and scenario.grade is not None:
            row["grade"] = {"status": scenario.grade, "checks": [{"key": "contains", "status": scenario.grade if scenario.grade != "ungraded" else "ungraded", "message": ""}]}
        rows.append(row)
    (batch_dir / "run_index.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    metadata = {
        "schema_version": 1,
        "batch_id": batch_id,
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(minutes=len(scenarios) + 1)).isoformat() if finished else None,
        "status": "completed",
        "config": {"turn_cap": 50, "grade": graded, "model": model},
        "tasks_path": "tests/golden/tasks.jsonl",
        "scenario_count": len(scenarios),
        "task_count": sum(len(scenario.tasks) for scenario in scenarios),
        "provenance": {
            "git": {"commit": "c" * 40, "branch": "main", "dirty": False},
            "model": model,
            "provider": "ollama",
            "tasks_sha256": tasks_hash or tasks_sha256(batch_dir / "tasks.jsonl"),
            "settings_sha256": "s" * 64,
            "non_default_sections": ["llm"],
            "python": "3.12.0",
            "platform": "win32",
        },
    }
    (batch_dir / "batch.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return batch_dir
