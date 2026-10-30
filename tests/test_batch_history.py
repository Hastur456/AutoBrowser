"""The run history: one row per scenario per batch run, rotated by size."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent_loop.batch_history import append_history, history_rows, load_history

METADATA = {
    "schema_version": 1,
    "batch_id": "batch-1",
    "started_at": "2026-10-29T12:00:00+00:00",
    "provenance": {
        "git": {"commit": "abc", "branch": "main", "dirty": True},
        "model": "gemma",
        "tasks_sha256": "t" * 64,
        "settings_sha256": "s" * 64,
    },
}


def _row(scenario: str, **extra: Any) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "failure_kind": "passed",
        "grade": {"status": "passed", "checks": []},
        "metrics": {"duration_ms": 1200, "model_calls": 3, "input_tokens": 900},
        "final_answer": "a long answer that must not reach the history",
        **extra,
    }


def test_a_history_row_holds_the_run_identity_and_the_scenario_figures() -> None:
    (row,) = history_rows(METADATA, [_row("books")])

    assert row == {
        "schema_version": 1,
        "batch_id": "batch-1",
        "started_at": "2026-10-29T12:00:00+00:00",
        "git": {"commit": "abc", "dirty": True},
        "model": "gemma",
        "tasks_sha256": "t" * 64,
        "settings_sha256": "s" * 64,
        "scenario_id": "books",
        "grade_status": "passed",
        "failure_kind": "passed",
        "duration_ms": 1200,
        "tasks": None,
        "model_calls": 3,
        "input_tokens": 900,
        "output_tokens": None,
        "model_ms": None,
        "tool_calls": None,
        "tool_ms": None,
        "tool_errors": None,
        "policy_blocks": None,
    }


def test_a_run_without_provenance_or_grade_still_gets_rows() -> None:
    (row,) = history_rows({"batch_id": "b"}, [{"scenario_id": "s", "metrics": None}])

    assert row["git"] == {"commit": None, "dirty": None}
    assert row["model"] is None
    assert row["grade_status"] is None
    assert row["failure_kind"] is None
    assert row["duration_ms"] is None


def test_rows_are_appended_and_loaded_back(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"

    append_history(path, [{"n": 1}, {"n": 2}], max_rows=100)
    append_history(path, [{"n": 3}], max_rows=100)

    assert load_history(path) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_the_history_keeps_only_the_newest_rows(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    append_history(path, [{"n": n} for n in range(5)], max_rows=100)

    append_history(path, [{"n": 5}, {"n": 6}], max_rows=3)

    assert load_history(path) == [{"n": 4}, {"n": 5}, {"n": 6}]
    assert not list(tmp_path.glob("*.tmp"))


def test_a_missing_history_loads_empty_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    assert load_history(path) == []

    path.write_text(json.dumps({"n": 1}) + "\n\n", encoding="utf-8")

    assert load_history(path) == [{"n": 1}]


def test_unicode_line_separators_inside_values_survive(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    value = "shop\u2028cart\x85"

    append_history(path, [{"scenario_id": value}, {"scenario_id": "next"}], max_rows=100)
    append_history(path, [{"scenario_id": "third"}], max_rows=3)

    assert load_history(path) == [{"scenario_id": value}, {"scenario_id": "next"}, {"scenario_id": "third"}]


def test_an_append_after_a_torn_last_line_starts_a_new_line(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"n": 1}\n{"n": 2', encoding="utf-8")

    append_history(path, [{"n": 3}], max_rows=100)

    assert path.read_text(encoding="utf-8").splitlines()[-1] == '{"n": 3}'
