"""``src/agent_loop/platform_records.py``: neutral dataset items and score records.

Pure: scenarios and batch rows in, records out. The platform bridge in ``scripts/`` maps them
to a vendor's objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

from src.agent_loop.batch import load_batch_scenarios
from src.agent_loop.batch_compare import BatchComparison, BatchFinding
from src.agent_loop.failures import FailureKind
from src.agent_loop.platform_records import (
    DatasetItemRecord,
    ScoreRecord,
    batch_score_records,
    golden_items,
    platform_key,
    score_definitions,
)
from src.contracts import StopReason

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "tasks.jsonl"


def write_tasks(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


# -- keys --------------------------------------------------------------------------------------


def test_platform_keys_are_deterministic_hex_of_the_joined_parts() -> None:
    key = platform_key("autobrowser/golden", "books-1")

    assert key == platform_key("autobrowser/golden", "books-1")
    assert len(key) == 32 and int(key, 16) >= 0
    assert key != platform_key("autobrowser/live", "books-1")
    assert platform_key("a\nb") == platform_key("a", "b")
    assert len(platform_key("x", length=16)) == 16


# -- dataset items -----------------------------------------------------------------------------


def test_golden_items_map_every_scenario_of_the_real_golden_set() -> None:
    scenarios = load_batch_scenarios(GOLDEN)

    items = golden_items(GOLDEN, dataset="autobrowser/golden", suite="golden", source_path="tests/golden/tasks.jsonl")

    assert [item.scenario_id for item in items] == [scenario.scenario_id for scenario in scenarios]
    for item, scenario in zip(items, scenarios, strict=True):
        assert item.item_key == f"autobrowser/golden\n{scenario.scenario_id}"
        assert item.input == {"tasks": scenario.tasks, "scenario_id": scenario.scenario_id}
        assert item.metadata == {
            "scenario_id": scenario.scenario_id,
            "tags": list(scenario.tags or []),
            "suite": "golden",
            "content_sha256": item.content_sha256,
            "source_path": "tests/golden/tasks.jsonl",
        }
        if scenario.expected is not None:
            assert item.expected == {"checks": scenario.expected, "reference": scenario.reference}


def test_an_item_without_expectations_has_no_expected_output(tmp_path: Path) -> None:
    path = write_tasks(tmp_path / "t.jsonl", [{"scenario_id": "s1", "tasks": ["a"]}, {"scenario_id": "s2", "tasks": ["b"], "reference": "rubric"}])

    first, second = golden_items(path, dataset="d", suite="golden", source_path="t.jsonl")

    assert first.expected is None
    assert second.expected == {"checks": None, "reference": "rubric"}


def test_the_content_hash_ignores_key_order_and_changes_with_content(tmp_path: Path) -> None:
    one = write_tasks(tmp_path / "one.jsonl", [{"scenario_id": "s1", "tasks": ["a"], "expected": {"contains": "x", "min_items": 2}, "tags": ["t"]}])
    two = write_tasks(tmp_path / "two.jsonl", [{"tags": ["t"], "expected": {"min_items": 2, "contains": "x"}, "tasks": ["a"], "scenario_id": "s1"}])
    three = write_tasks(tmp_path / "three.jsonl", [{"scenario_id": "s1", "tasks": ["a"], "expected": {"contains": "y", "min_items": 2}, "tags": ["t"]}])
    four = write_tasks(tmp_path / "four.jsonl", [{"scenario_id": "s1", "tasks": ["a"], "expected": {"contains": "x", "min_items": 2}, "tags": ["t"], "reference": "r"}])

    hashes = [golden_items(path, dataset="d", suite="golden", source_path="p")[0].content_sha256 for path in (one, two, three, four)]

    assert hashes[0] == hashes[1]
    assert len({hashes[0], hashes[2], hashes[3]}) == 3


def test_the_content_hash_does_not_depend_on_the_dataset_or_path(tmp_path: Path) -> None:
    path = write_tasks(tmp_path / "t.jsonl", [{"scenario_id": "s1", "tasks": ["a"]}])

    first = golden_items(path, dataset="d1", suite="golden", source_path="p1")[0]
    second = golden_items(path, dataset="d2", suite="golden", source_path="p2")[0]

    assert isinstance(first, DatasetItemRecord)
    assert first.content_sha256 == second.content_sha256
    assert first.item_key != second.item_key


# -- score definitions -------------------------------------------------------------------------


def test_score_definitions_take_their_categories_from_the_code_vocabulary() -> None:
    definitions = {definition.name: definition for definition in score_definitions()}

    assert definitions["failure_kind"].data_type == "categorical"
    assert definitions["failure_kind"].categories == get_args(FailureKind)
    assert definitions["task_failure_kind"].categories == get_args(FailureKind)
    assert definitions["stop_reason"].categories == get_args(StopReason)
    assert definitions["passed"].data_type == "boolean"
    assert definitions["infra_failure"].data_type == "boolean"
    assert definitions["grade"].categories == ("passed", "failed", "ungraded")
    assert definitions["check.contains"].categories == ("passed", "failed", "ungraded")
    assert {"check.run", "check.expected", "check.min_items"} <= set(definitions)
    assert definitions["regression"].categories == ("regression", "fixed", "inconclusive", "changed", "added", "unchanged")
    assert definitions["verdict"].categories == ("no_regression", "regression", "incomparable", "no_baseline")
    assert (definitions["pass_rate"].min_value, definitions["pass_rate"].max_value) == (0.0, 1.0)
    for name in ("model_calls", "tool_calls", "tool_errors", "policy_blocks", "time_to_first_tool_ms", "agent_failures", "infra_failures", "regressions"):
        assert definitions[name].data_type == "numeric"
    assert all(definition.description for definition in definitions.values())


# -- score records -----------------------------------------------------------------------------


META = {"batch_id": "batch-1", "finished_at": "2026-11-01T12:00:00+00:00"}


def row(scenario_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "failure_kind": kind,
        "metrics": {"duration_ms": 9000, "model_calls": 4, "tool_calls": 3, "tool_errors": 0, "policy_blocks": 0, "time_to_first_tool_ms": None},
        "task_outcomes": [{"task_id": f"{scenario_id}-t1", "status": "completed", "stop_reason": "answered", "error_type": "", "permission_denies": 0, "failure_kind": kind if kind != "ungraded" else "passed"}],
        **extra,
    }


def by(records: list[ScoreRecord], name: str) -> list[ScoreRecord]:
    return [record for record in records if record.name == name]


def test_item_scores_of_a_graded_run() -> None:
    graded = row(
        "s1",
        "grade_failed",
        grade={"status": "failed", "checks": [
            {"key": "contains", "status": "passed", "message": ""},
            {"key": "min_items", "status": "failed", "message": "2 items, expected at least 3"},
            {"key": "fields", "status": "ungraded", "message": "no deterministic grader for this key"},
        ]},
    )

    records = batch_score_records(META, [graded], None)
    item = {record.name: record for record in records if record.target == "item"}

    assert item["passed"] == ScoreRecord("passed", "boolean", False, "item", scenario_id="s1")
    assert item["failure_kind"].value == "grade_failed"
    assert item["infra_failure"].value is False
    assert item["grade"].value == "failed"
    assert item["check.contains"].value == "passed"
    assert item["check.min_items"].value == "failed"
    assert item["check.min_items"].comment == "2 items, expected at least 3"
    assert "check.fields" not in item
    assert item["model_calls"].value == 4.0 and item["model_calls"].data_type == "numeric"
    assert "time_to_first_tool_ms" not in item and "duration_ms" not in item
    assert "regression" not in item


def test_an_ungraded_run_sends_no_grade_or_check_scores() -> None:
    records = batch_score_records(META, [row("s1", "passed")], None)

    assert not by(records, "grade") and not [record for record in records if record.name.startswith("check.")]
    assert by(records, "passed")[0].value is True


def test_an_infra_failure_and_a_run_without_a_trace() -> None:
    no_trace = {"scenario_id": "s2", "failure_kind": "provider_error", "metrics": {"duration_ms": 10}, "task_outcomes": []}

    records = batch_score_records(META, [no_trace], None)

    assert by(records, "infra_failure")[0].value is True
    assert not by(records, "model_calls") and not by(records, "stop_reason")


def test_task_scores_target_each_task() -> None:
    blocked = row("s1", "permission_denied")
    blocked["task_outcomes"] = [
        {"task_id": "t1", "status": "completed", "stop_reason": "answered", "error_type": "", "permission_denies": 0, "failure_kind": "passed"},
        {"task_id": "t2", "status": "blocked", "stop_reason": "", "error_type": "", "permission_denies": 1, "failure_kind": "permission_denied"},
        {"task_id": "", "status": "missing", "stop_reason": "", "error_type": "", "permission_denies": 0, "failure_kind": "error"},
    ]

    records = batch_score_records(META, [blocked], None)

    assert [(r.task_id, r.value) for r in by(records, "task_failure_kind")] == [("t1", "passed"), ("t2", "permission_denied")]
    assert [(r.task_id, r.value) for r in by(records, "stop_reason")] == [("t1", "answered")]
    assert all(r.target == "task" and r.scenario_id == "s1" for r in by(records, "task_failure_kind"))


def test_run_scores_without_a_baseline() -> None:
    rows = [row("s1", "passed"), row("s2", "grade_failed"), row("s3", "timeout"), row("s4", "ungraded")]

    run = {r.name: r for r in batch_score_records(META, rows, None) if r.target == "run"}

    assert run["pass_rate"].value == 0.25
    assert run["agent_failures"].value == 1.0
    assert run["infra_failures"].value == 1.0
    assert run["verdict"].value == "no_baseline"
    assert "regressions" not in run


def comparison(*findings: BatchFinding, comparable: bool = True) -> BatchComparison:
    return BatchComparison(batch_a="batch-0", batch_b="batch-1", comparable=comparable, findings=list(findings), unchanged=1)


def test_the_local_verdict_becomes_item_and_run_scores() -> None:
    rows = [row("s1", "grade_failed"), row("s2", "passed"), row("s3", "passed")]
    found = comparison(
        BatchFinding("regression", "s1", "passed", "grade_failed"),
        BatchFinding("slower", "s2", 10, 20, metric="duration_ms", change=1.0),
        BatchFinding("added", "s3", None, "passed"),
        BatchFinding("removed", "s9", "passed", None),
    )

    records = batch_score_records(META, rows, found)

    assert {r.scenario_id: r.value for r in by(records, "regression")} == {"s1": "regression", "s2": "unchanged", "s3": "added"}
    run = {r.name: r.value for r in records if r.target == "run"}
    assert run["verdict"] == "regression"
    assert run["regressions"] == 1.0


def test_a_clean_or_incomparable_comparison_verdict() -> None:
    rows = [row("s1", "passed")]

    clean = {r.name: r.value for r in batch_score_records(META, rows, comparison()) if r.target == "run"}
    apart = {r.name: r.value for r in batch_score_records(META, rows, comparison(comparable=False)) if r.target == "run"}

    assert clean["verdict"] == "no_regression" and clean["regressions"] == 0.0
    assert apart["verdict"] == "incomparable" and "regressions" not in apart
    assert not by(batch_score_records(META, rows, comparison(comparable=False)), "regression")


def test_every_record_names_a_defined_score_with_a_valid_value() -> None:
    definitions = {definition.name: definition for definition in score_definitions()}
    rows = [
        row("s1", "grade_failed", grade={"status": "failed", "checks": [{"key": "run", "status": "failed", "message": "x"}]}),
        row("s2", "passed"),
    ]

    for record in batch_score_records(META, rows, comparison(BatchFinding("fixed", "s2", "error", "passed"))):
        definition = definitions[record.name]
        assert record.data_type == definition.data_type
        if definition.categories:
            assert record.value in definition.categories
