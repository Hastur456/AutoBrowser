"""The verdict between two batch runs, from their metadata and rows."""

from __future__ import annotations

from typing import Any

import pytest

from src.agent_loop.batch_compare import BatchComparison, compare_batches
from src.config import PipelineSettings

SETTINGS = PipelineSettings(batch_metric_tolerance=0.25)


def _meta(batch_id: str, *, tasks: str = "t", settings: str = "s", model: str = "m") -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "provenance": {"tasks_sha256": tasks, "settings_sha256": settings, "model": model},
    }


def _row(scenario: str, kind: str, **metrics: Any) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "failure_kind": kind,
        "metrics": {"duration_ms": 10_000, "model_calls": 10, "input_tokens": 50_000, **metrics},
    }


def _compare(rows_a: list[dict[str, Any]], rows_b: list[dict[str, Any]], **meta_b: Any) -> BatchComparison:
    return compare_batches(_meta("a"), rows_a, _meta("b", **meta_b), rows_b, SETTINGS)


def _kinds(comparison: BatchComparison) -> dict[str, str]:
    return {finding.scenario_id: finding.kind for finding in comparison.findings}


@pytest.mark.parametrize(
    ("before", "after", "finding"),
    [
        ("passed", "guard_limit", "regression"),
        ("passed", "grade_failed", "regression"),
        ("passed", "permission_denied", "regression"),
        ("guard_limit", "passed", "fixed"),
        ("passed", "provider_error", "inconclusive"),
        ("guard_limit", "timeout", "inconclusive"),
        ("provider_error", "passed", "changed"),
        ("guard_limit", "agent_stopped", "changed"),
        ("passed", "ungraded", "changed"),
    ],
)
def test_a_change_of_failure_kind_is_classified(before: str, after: str, finding: str) -> None:
    comparison = _compare([_row("s1", before)], [_row("s1", after)])

    (only,) = comparison.findings
    assert (only.kind, only.before, only.after) == (finding, before, after)


def test_scenarios_in_one_run_only_are_added_or_removed() -> None:
    comparison = _compare([_row("old", "passed"), _row("both", "passed")], [_row("both", "passed"), _row("new", "passed")])

    assert _kinds(comparison) == {"old": "removed", "new": "added"}
    assert comparison.unchanged == 1


def test_an_unchanged_run_has_no_findings_and_exits_0() -> None:
    comparison = _compare([_row("s1", "passed"), _row("s2", "guard_limit")], [_row("s1", "passed"), _row("s2", "guard_limit")])

    assert comparison.findings == []
    assert comparison.unchanged == 2
    assert comparison.exit_code() == 0


def test_a_regression_exits_1_and_an_infra_failure_alone_exits_0() -> None:
    regressed = _compare([_row("s1", "passed")], [_row("s1", "guard_limit")])
    outage = _compare([_row("s1", "passed")], [_row("s1", "provider_error")])

    assert regressed.exit_code() == 1
    assert outage.exit_code() == 0


@pytest.mark.parametrize(
    ("metrics", "findings"),
    [
        ({"duration_ms": 13_000}, {("slower", "duration_ms")}),
        ({"model_calls": 13}, {("costlier", "model_calls")}),
        ({"input_tokens": 70_000}, {("costlier", "input_tokens")}),
        ({"duration_ms": 12_000, "model_calls": 12, "input_tokens": 60_000}, set()),
        ({"duration_ms": 5_000}, set()),
    ],
)
def test_metrics_of_scenarios_passed_in_both_runs_are_compared(
    metrics: dict[str, Any], findings: set[tuple[str, str]]
) -> None:
    comparison = _compare([_row("s1", "passed")], [_row("s1", "passed", **metrics)])

    assert {(f.kind, f.metric) for f in comparison.findings} == findings


def test_metrics_are_not_compared_for_a_failed_scenario_or_a_missing_value() -> None:
    failed = _compare([_row("s1", "guard_limit")], [_row("s1", "guard_limit", duration_ms=99_000)])
    missing = _compare(
        [_row("s1", "passed", input_tokens=None)], [_row("s1", "passed", input_tokens=99_000)]
    )
    zero = _compare([_row("s1", "passed", model_calls=0)], [_row("s1", "passed", model_calls=5)])

    assert failed.findings == missing.findings == zero.findings == []


def test_perf_findings_fail_only_under_the_perf_gate() -> None:
    comparison = _compare([_row("s1", "passed")], [_row("s1", "passed", duration_ms=30_000)])

    assert comparison.exit_code() == 0
    assert comparison.exit_code(perf_gate=True) == 3


def test_a_regression_outranks_the_perf_gate() -> None:
    comparison = _compare(
        [_row("s1", "passed"), _row("s2", "passed")],
        [_row("s1", "guard_limit"), _row("s2", "passed", duration_ms=30_000)],
    )

    assert comparison.exit_code(perf_gate=True) == 1


def test_different_golden_sets_are_incomparable() -> None:
    comparison = _compare([_row("s1", "passed")], [_row("s1", "guard_limit")], tasks="other")

    assert comparison.comparable is False
    assert comparison.findings == []
    assert comparison.exit_code() == 2


def test_a_run_without_a_golden_set_hash_is_incomparable() -> None:
    comparison = compare_batches({"batch_id": "a"}, [], _meta("b"), [], SETTINGS)

    assert comparison.comparable is False
    assert comparison.exit_code() == 2


def test_different_settings_or_models_are_warnings() -> None:
    comparison = _compare([_row("s1", "passed")], [_row("s1", "passed")], settings="other", model="other-model")

    assert comparison.comparable is True
    assert len(comparison.warnings) == 2
    assert any("settings" in warning for warning in comparison.warnings)
    assert any("other-model" in warning for warning in comparison.warnings)


def test_findings_describe_themselves_and_render_as_markdown() -> None:
    comparison = _compare(
        [_row("s1", "passed"), _row("s2", "passed")],
        [_row("s1", "guard_limit"), _row("s2", "passed", duration_ms=20_000)],
    )

    lines = [finding.describe() for finding in comparison.findings]
    assert "regression: s1 passed -> guard_limit" in lines
    assert "slower: s2 duration_ms 10000 -> 20000 (+100%)" in lines
    markdown = comparison.render_markdown()
    assert markdown.startswith("# Batch comparison")
    assert "a" in markdown and "b" in markdown
    assert "| regression | s1 | passed | guard_limit |" in markdown
    payload = comparison.to_json_dict()
    assert payload["exit_code"] == 1
    assert payload["findings"][0]["kind"] == "regression"
