"""Baseline comparison rules: exact counters, the request size tolerance, suite integrity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.comparison import (
    BaselineError,
    Finding,
    MetricRule,
    baseline_document,
    compare,
    deterministic_rules,
    load_baseline,
)
from src.config import PipelineSettings

RULES = deterministic_rules(PipelineSettings(request_chars_tolerance=0.10))


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "terminal_status": "completed",
        "trace_complete": True,
        "final_answer": "Done.",
        "model_turn_count": 4,
        "tool_call_count": 2,
        "policy_block_count": 0,
        "repeated_action_count": 0,
        "approval_request_count": 0,
        "error_count": 0,
        "observation_count": 2,
        "action_signature": "sha256:abc",
        "request_chars": 1000,
    }
    row.update(overrides)
    return row


def _kinds(report: Any) -> list[tuple[str, str, str]]:
    return [(finding.kind, finding.scenario, finding.metric) for finding in report.findings]


def test_identical_results_have_no_findings() -> None:
    report = compare({"a": _row()}, {"a": _row()}, RULES, suite="evals")

    assert report.findings == ()
    assert report.failed is False


@pytest.mark.parametrize(
    "metric, value",
    [
        ("terminal_status", "blocked"),
        ("final_answer", "Other."),
        ("tool_call_count", 3),
        ("error_count", 1),
        ("action_signature", "sha256:def"),
        ("trace_complete", False),
    ],
)
def test_any_difference_of_an_exact_metric_is_a_failing_change(metric: str, value: Any) -> None:
    report = compare({"a": _row(**{metric: value})}, {"a": _row()}, RULES, suite="evals")

    assert _kinds(report) == [("changed", "a", metric)]
    assert report.failed is True


def test_a_changed_counter_says_which_way_it_moved() -> None:
    fewer = compare({"a": _row(tool_call_count=1)}, {"a": _row()}, RULES, suite="evals")
    more = compare({"a": _row(tool_call_count=3)}, {"a": _row()}, RULES, suite="evals")

    assert "probably better" in fewer.findings[0].message
    assert "more than the baseline" in more.findings[0].message
    assert fewer.failed and more.failed


def test_request_chars_within_the_tolerance_is_no_finding() -> None:
    report = compare({"a": _row(request_chars=1100)}, {"a": _row()}, RULES, suite="evals")

    assert report.findings == ()


def test_request_chars_growth_past_the_tolerance_is_a_regression() -> None:
    report = compare({"a": _row(request_chars=1101)}, {"a": _row()}, RULES, suite="evals")

    (finding,) = report.findings
    assert (finding.kind, finding.metric) == ("regression", "request_chars")
    assert "more expensive" in finding.message
    assert report.failed is True


def test_a_request_chars_drop_past_the_tolerance_is_a_passing_improvement() -> None:
    report = compare({"a": _row(request_chars=800)}, {"a": _row()}, RULES, suite="evals")

    assert _kinds(report) == [("improvement", "a", "request_chars")]
    assert report.failed is False


def test_request_chars_from_a_zero_baseline_is_a_regression() -> None:
    rule = MetricRule("request_chars", "ratio", tolerance=0.5)

    report = compare({"a": {"request_chars": 1}}, {"a": {"request_chars": 0}}, [rule], suite="s")

    assert _kinds(report) == [("regression", "a", "request_chars")]


def test_a_scenario_without_a_row_and_a_row_without_a_scenario_both_fail() -> None:
    report = compare({"new": _row()}, {"gone": _row()}, RULES, suite="evals")

    assert _kinds(report) == [("orphan_baseline", "gone", ""), ("missing_baseline", "new", "")]
    assert report.failed is True


def test_a_metric_missing_from_the_baseline_row_is_a_change() -> None:
    row = _row()
    del row["observation_count"]

    report = compare({"a": _row()}, {"a": row}, RULES, suite="evals")

    (finding,) = report.findings
    assert finding.kind == "changed"
    assert "missing from the baseline" in finding.message


def test_findings_describe_themselves_in_one_line() -> None:
    changed = Finding("changed", "evals", "a", metric="tool_call_count", baseline=2, actual=3)
    assertion = Finding("regression", "evals", "a", metric="assertions", message="too many calls")

    assert changed.describe() == "a.tool_call_count: changed 2 -> 3"
    assert assertion.describe() == "a.assertions: regression (too many calls)"


def test_the_tolerance_comes_from_the_settings() -> None:
    (rule,) = [
        rule
        for rule in deterministic_rules(PipelineSettings(request_chars_tolerance=0.5))
        if rule.metric == "request_chars"
    ]

    assert (rule.kind, rule.tolerance) == ("ratio", 0.5)


def test_a_baseline_document_round_trips_through_load_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    document = baseline_document("agent_loop", {"b": _row(), "a": _row()}, {"a": _row()})
    path.write_text(json.dumps(document), encoding="utf-8")

    baseline = load_baseline(path)

    assert list(document["scenarios"]) == ["a", "b"]
    assert baseline.suite == "agent_loop"
    assert baseline.scenarios["a"] == _row()
    assert baseline.scenarios_with_memory == {"a": _row()}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"a": {"tool_call_count": 1}}),
        json.dumps({"schema_version": 2, "scenarios": []}),
        json.dumps({"schema_version": 2, "scenarios": {}, "scenarios_with_memory": {"a": 1}}),
    ],
)
def test_a_malformed_baseline_is_a_baseline_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(BaselineError):
        load_baseline(path)


def test_a_missing_baseline_is_a_baseline_error(tmp_path: Path) -> None:
    with pytest.raises(BaselineError):
        load_baseline(tmp_path / "absent.json")
