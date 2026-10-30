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


def _row(scenario: str, grade: dict[str, Any] | None, kind: str | None = None, **metrics: Any) -> dict[str, Any]:
    if kind is None:
        kind = {None: "ungraded", "passed": "passed", "failed": "grade_failed"}[(grade or {}).get("status")]
    return {
        "scenario_id": scenario,
        "failure_kind": kind,
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
    assert "| Scenario | Grade | Kind | Tasks done |" in report
    assert "| books | passed | passed | 2/2 | 12.3 | 9 | 52 000 / 1 200 | 30.1 | 6 | 8.0 | 0 | 0 |" in report
    assert "| ozon | failed | grade_failed | 2/2 | 12.3 | 4 | — / — | — | — | — | — | — |" in report
    assert "| **Total** | 1/2 passed | — | 4/4 | 24.7 | 13 | 52 000 / 1 200 |" in report


def test_failed_scenarios_list_their_failed_checks_and_answer() -> None:
    report = render_batch_report(METADATA, [_row("books", PASSED), _row("ozon", FAILED)], graded=True)

    failures = report.split("## Failed scenarios", 1)[1]
    assert "#### ozon" in failures
    assert "#### books" not in failures
    assert "- `regex`: no match for ['x']" in failures
    assert "`items`" not in failures
    assert "> Line one\n> Line two" in failures
    assert "Trace: session `sessions/ozon`" in failures


def test_an_ungraded_run_shows_no_grades() -> None:
    report = render_batch_report(METADATA, [_row("books", None)], graded=False)

    assert "Grades:" not in report
    assert "| books | — | ungraded | 2/2 |" in report
    assert "## Failed scenarios" not in report


def test_the_report_lists_the_trace_of_every_scenario() -> None:
    report = render_batch_report(METADATA, [_row("books", PASSED), _row("ozon", FAILED)], graded=True)

    traces = report.split("## Traces", 1)[1]
    assert "- books: session `sessions/books`" in traces
    assert "- ozon: session `sessions/ozon`" in traces


def test_the_report_links_the_readable_trace_before_the_session() -> None:
    row = {**_row("ozon", FAILED), "trace_text_path": "traces/ozon.txt"}

    report = render_batch_report(METADATA, [row], graded=True)

    link = "[traces/ozon.txt](traces/ozon.txt) · session `sessions/ozon`"
    assert f"Trace: {link}" in report.split("## Failed scenarios", 1)[1]
    assert f"- ozon: {link}" in report.split("## Traces", 1)[1]




PROVENANCE = {
    "git": {"commit": "0123456789abcdef0123", "branch": "main", "dirty": True},
    "model": "gemma4:31b-cloud",
    "settings_sha256": "f" * 64,
    "non_default_sections": ["llm", "permissions"],
}


def test_the_report_states_the_provenance_of_the_run() -> None:
    report = render_batch_report({**METADATA, "provenance": PROVENANCE}, [_row("books", PASSED)], graded=True)

    assert "- Commit: 0123456789ab (dirty)" in report
    assert "- Model: gemma4:31b-cloud" in report
    assert "- Settings: ffffffffffff, non-default: llm, permissions" in report


def test_a_report_without_provenance_falls_back_to_the_config_model() -> None:
    report = render_batch_report(METADATA, [_row("books", PASSED)], graded=True)

    assert "- Model: test-model" in report
    assert "Commit:" not in report


def test_the_summary_separates_agent_and_infra_failures() -> None:
    rows = [
        _row("a", PASSED),
        _row("b", FAILED),
        _row("c", None, kind="provider_error"),
        _row("d", None, kind="guard_limit"),
        _row("e", None, kind="ungraded"),
    ]

    report = render_batch_report(METADATA, rows, graded=True)

    assert "- Agent failures: 2 · Infra failures: 1 · Passed: 1" in report


def test_failed_scenarios_are_grouped_by_kind_with_infra_last() -> None:
    rows = [
        _row("outage", None, kind="timeout"),
        _row("wrong", FAILED),
        _row("loop", None, kind="guard_limit"),
        _row("fine", PASSED),
        _row("open", None, kind="ungraded"),
    ]

    report = render_batch_report(METADATA, rows, graded=True)
    failures = report.split("## Failed scenarios", 1)[1].split("## Traces", 1)[0]

    headings = [line for line in failures.splitlines() if line.startswith("### ")]
    assert headings == ["### guard_limit (1)", "### grade_failed (1)", "### timeout (1)"]
    assert failures.index("#### loop") < failures.index("#### wrong") < failures.index("#### outage")
    assert "fine" not in failures and "open" not in failures
    infra = failures.split("### timeout (1)", 1)[1]
    assert "rerun" in infra.lower()


def test_a_row_without_a_failure_kind_falls_back_to_its_grade() -> None:
    legacy = {key: value for key, value in _row("old", FAILED).items() if key != "failure_kind"}

    report = render_batch_report(METADATA, [legacy], graded=True)

    assert "| old | failed | — | 2/2 |" in report
    assert "#### old" in report.split("## Failed scenarios", 1)[1]
