"""The markdown report of a batch run."""

from __future__ import annotations

from typing import Any

from src.agent_loop.batch_report import render_batch_report, total_metrics

METADATA = {
    "batch_id": "batch-1",
    "started_at": "2026-10-28T12:00:00+00:00",
    "finished_at": "2026-10-28T12:05:00+00:00",
    "status": "completed",
    "config": {"model": "test-model"},
}


def _row(scenario: str, grade: dict[str, Any] | None, **metrics: Any) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "tasks": ["a", "b"],
        "task_statuses": ["done", "done"],
        "final_answer": "Line one\nLine two",
        "session_dir": f"sessions/{scenario}",
        "grade": grade,
        "metrics": {"duration_ms": 12_345, **metrics},
    }


PASSED = {"status": "passed", "checks": [{"key": "contains", "status": "passed", "message": ""}]}
FAILED = {
    "status": "failed",
    "checks": [
        {"key": "items", "status": "passed", "message": ""},
        {"key": "regex", "status": "failed", "message": "no match for ['x']"},
    ],
}


def test_total_metrics_add_up_and_keep_unreported_metrics_none() -> None:
    rows = [
        _row("a", PASSED, model_calls=3, input_tokens=1000),
        _row("b", FAILED, model_calls=4),
        {"scenario_id": "c", "metrics": None},
    ]

    totals = total_metrics(rows)

    assert totals["duration_ms"] == 24_690
    assert totals["model_calls"] == 7
    assert totals["input_tokens"] == 1000
    assert totals["output_tokens"] is None


def test_the_report_has_a_row_per_scenario_and_a_total() -> None:
    rows = [
        _row("books", PASSED, model_calls=9, input_tokens=52_000, output_tokens=1_200, model_ms=30_100, tool_calls=6, tool_ms=8_000, tool_errors=0, policy_blocks=0),
        _row("ozon", FAILED, model_calls=4),
    ]

    report = render_batch_report(METADATA, rows, graded=True)

    assert report.startswith("# Batch report: batch-1\n")
    assert "- Model: test-model" in report
    assert "- Grades: 1 passed, 1 failed, 0 ungraded" in report
    assert "| books | passed | 2/2 | 12.3 | 9 | 52 000 / 1 200 | 30.1 | 6 | 8.0 | 0 | 0 |" in report
    assert "| ozon | failed | 2/2 | 12.3 | 4 | — / — | — | — | — | — | — |" in report
    assert "| **Total** | 1/2 passed | 4/4 | 24.7 | 13 | 52 000 / 1 200 |" in report


def test_failed_scenarios_list_their_failed_checks_and_answer() -> None:
    report = render_batch_report(METADATA, [_row("books", PASSED), _row("ozon", FAILED)], graded=True)

    failures = report.split("## Failed scenarios", 1)[1]
    assert "### ozon" in failures
    assert "### books" not in failures
    assert "- `regex`: no match for ['x']" in failures
    assert "`items`" not in failures
    assert "> Line one\n> Line two" in failures
    assert "Trace: `sessions/ozon`" in failures


def test_an_ungraded_run_shows_no_grades() -> None:
    report = render_batch_report(METADATA, [_row("books", None)], graded=False)

    assert "Grades:" not in report
    assert "| books | — | 2/2 |" in report
    assert "## Failed scenarios" not in report
