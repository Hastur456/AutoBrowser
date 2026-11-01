"""``scripts/langfuse_bridge/experiments.py``: one batch scenario as one Langfuse item trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.langfuse_bridge.client import LangfuseClient
from scripts.langfuse_bridge.datasets import item_id
from scripts.langfuse_bridge.experiments import (
    ExperimentRef,
    ItemTrace,
    experiment_description,
    experiment_id,
    experiment_name,
    item_trace,
    item_trace_id,
    load_batch_folder,
)
from tests.platform_fixtures.batch_folder import (
    ANSWER_TEXT,
    TOOL_RESULT_TEXT,
    Scenario,
    make_batch,
)
from tests.platform_fixtures.fake_langfuse import FakeLangfuse
from tests.test_langfuse_client import settings

DATASET = "autobrowser/golden"


def ref(**overrides: Any) -> ExperimentRef:
    values: dict[str, Any] = {
        "id": "batch-1a2b3c4d5e6f",
        "name": "golden · run",
        "dataset_name": DATASET,
        "dataset_id": "dataset-1",
        "item_version": "2026-11-01T10:00:00Z",
        "version_matched": True,
        "baseline_batch_id": "batch-0",
    }
    values.update(overrides)
    return ExperimentRef(**values)


def build(tmp_path: Path, scenarios: list[Scenario], *, capture: str = "none", **overrides: Any) -> list[ItemTrace]:
    folder = load_batch_folder(make_batch(tmp_path, scenarios), dataset=DATASET, suite="golden")
    platform = settings(capture_content=capture, **overrides)
    return [item_trace(folder, row, ref(), platform, service_name="autobrowser") for row in folder.rows]


def spans_of(trace: ItemTrace) -> list[dict[str, Any]]:
    return [span for payload in trace.payloads for rs in payload["resourceSpans"] for ss in rs["scopeSpans"] for span in ss["spans"]]


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def test_ids_names_and_description() -> None:
    meta = {
        "batch_id": "batch-1a2b3c4d5e6f",
        "started_at": "2026-10-31T14:05:59+00:00",
        "provenance": {"git": {"commit": "a1b2c3d4e5", "dirty": True}, "model": "gemma4:31b-cloud", "provider": "ollama"},
    }

    assert experiment_id("batch-1", 1) == "batch-1" and experiment_id("batch-1", 2) == "batch-1.2"
    assert experiment_name(meta, "golden") == "golden · 2026-10-31 14:05 · a1b2c3d* · gemma4:31b-cloud · batch-1a2b3c"
    assert item_trace_id("batch-1", "s1") == item_trace_id("batch-1", "s1")
    assert len(item_trace_id("batch-1", "s1")) == 32 and item_trace_id("batch-1", "s1") != item_trace_id("batch-1.2", "s1")
    rows = [{"failure_kind": "passed"}, {"failure_kind": "grade_failed"}, {"failure_kind": "timeout"}, {"failure_kind": "ungraded"}]
    description = experiment_description(meta, rows)
    assert "a1b2c3d" in description and "gemma4:31b-cloud" in description
    assert "Agent failures: 1 · Infra failures: 1 · Passed: 2" in description


def test_one_trace_per_scenario_with_the_task_tree(tmp_path: Path) -> None:
    traces = build(tmp_path, [Scenario("books-1", tasks=["open", "find"]), Scenario("quotes-1")])

    assert [trace.scenario_id for trace in traces] == ["books-1", "quotes-1"]
    first = spans_of(traces[0])
    assert {span["traceId"] for span in first} == {traces[0].trace_id}
    root = first[0]
    assert root["name"] == "scenario books-1" and "parentSpanId" not in root
    assert root["spanId"] == traces[0].root_span_id
    tasks = [span for span in first if span["name"] == "invoke_agent autobrowser"]
    assert len(tasks) == 2 and all(span["parentSpanId"] == root["spanId"] for span in tasks)
    assert any(span["name"] == "execute_tool click" for span in first)
    assert len({span["spanId"] for span in first}) == len(first)
    assert int(root["startTimeUnixNano"]) <= min(int(span["startTimeUnixNano"]) for span in first)
    assert int(root["endTimeUnixNano"]) >= max(int(span["endTimeUnixNano"]) for span in first)


def test_experiment_attributes_sit_on_every_span(tmp_path: Path) -> None:
    trace = build(tmp_path, [Scenario("books-1")])[0]

    for span in spans_of(trace):
        values = attrs(span)
        assert values["langfuse.experiment.id"] == "batch-1a2b3c4d5e6f"
        assert values["langfuse.experiment.name"] == "golden · run"
        assert values["langfuse.experiment.dataset.id"] == "dataset-1"
        assert values["langfuse.experiment.item.id"] == item_id(DATASET, "books-1")
        assert values["langfuse.experiment.item.root_observation_id"] == trace.root_span_id
        assert values["session.id"] == "session-books-1"
        assert values["langfuse.environment"] == "batch"
        assert values["autobrowser.batch.id"] == "batch-1a2b3c4d5e6f"
        assert values["autobrowser.scenario.id"] == "books-1"
        assert values["vcs.ref.head.revision"] == "c" * 40


def test_the_root_carries_the_item_and_the_experiment(tmp_path: Path) -> None:
    trace = build(tmp_path, [Scenario("books-1", expected={"contains": "£", "fields": ["price"]}, reference="One price.")])[0]

    root = attrs(spans_of(trace)[0])
    assert root["langfuse.observation.type"] == "agent"
    assert json.loads(root["langfuse.observation.input"]) == {"tasks": ["find the price"], "scenario_id": "books-1"}
    assert json.loads(root["langfuse.experiment.item.expected_output"]) == {"checks": {"contains": "£", "fields": ["price"]}, "reference": "One price."}
    assert root["langfuse.experiment.item.version"] == "2026-11-01T10:00:00Z"
    assert root["langfuse.experiment.item.metadata.scenario_id"] == "books-1"
    assert root["langfuse.experiment.metadata.batch_id"] == "batch-1a2b3c4d5e6f"
    assert root["langfuse.experiment.metadata.model"] == "gemma4:31b-cloud"
    assert root["langfuse.experiment.metadata.dataset_version_matched"] == "true"
    assert root["langfuse.experiment.metadata.baseline_batch_id"] == "batch-0"
    assert "langfuse.experiment.description" in root


def test_the_expected_output_comes_from_the_batch_copy(tmp_path: Path) -> None:
    batch = make_batch(tmp_path, [Scenario("books-1", expected={"contains": "old"})])
    (tmp_path / "repo-tasks.jsonl").write_text('{"scenario_id": "books-1", "tasks": ["x"], "expected": {"contains": "new"}}\n', encoding="utf-8")

    folder = load_batch_folder(batch, dataset=DATASET, suite="golden")
    trace = item_trace(folder, folder.rows[0], ref(), settings(), service_name="autobrowser")

    assert json.loads(attrs(spans_of(trace)[0])["langfuse.experiment.item.expected_output"])["checks"] == {"contains": "old"}


def test_a_failure_is_an_error_root_with_its_kind(tmp_path: Path) -> None:
    trace = build(tmp_path, [Scenario("books-1", failure_kind="grade_failed", grade="failed")])[0]

    root = spans_of(trace)[0]
    assert root["status"] == {"code": 2, "message": "grade_failed"}
    assert build(tmp_path / "ok", [Scenario("b", failure_kind="ungraded", grade="ungraded")])[0].payloads[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"]["code"] == 0


@pytest.mark.parametrize("capture", ["none", "answers", "redacted"])
def test_content_modes(tmp_path: Path, capture: str) -> None:
    trace = build(tmp_path, [Scenario("books-1")], capture=capture)[0]
    spans = spans_of(trace)
    values = [str(value) for span in spans for value in attrs(span).values()]
    root = attrs(spans[0])
    task = attrs(next(span for span in spans if span["name"] == "invoke_agent autobrowser"))
    tool = attrs(next(span for span in spans if span["name"] == "execute_tool click"))

    if capture == "none":
        assert not any(ANSWER_TEXT in value for value in values)
        assert json.loads(root["langfuse.observation.output"]) == {"failure_kind": "passed", "grade": "passed", "task_statuses": ["done"]}
        assert "input.value" not in task
    else:
        assert root["langfuse.observation.output"] == ANSWER_TEXT
        assert task["input.value"] == "find the price" and task["output.value"] == ANSWER_TEXT
    if capture == "redacted":
        assert tool["gen_ai.tool.call.result"] == TOOL_RESULT_TEXT
    else:
        assert not any(TOOL_RESULT_TEXT in value for value in values)


def test_a_scenario_without_a_trace_is_a_lone_root(tmp_path: Path) -> None:
    trace = build(tmp_path, [Scenario("books-1", traced=False, failure_kind="provider_error", grade="failed", error={"type": "ModelProviderError", "message": "502 " + ANSWER_TEXT})])[0]

    spans = spans_of(trace)
    assert len(spans) == 1
    root = attrs(spans[0])
    assert json.loads(root["langfuse.observation.output"])["error"] == {"type": "ModelProviderError"}
    assert ANSWER_TEXT not in " ".join(str(value) for value in root.values())
    assert spans[0]["startTimeUnixNano"] < spans[0]["endTimeUnixNano"]
    assert "session.id" not in root


def test_a_large_trace_is_split_without_repeating_a_span(tmp_path: Path) -> None:
    whole = build(tmp_path / "whole", [Scenario("books-1", tool_calls=40)])[0]
    split = build(tmp_path / "split", [Scenario("books-1", tool_calls=40)], max_request_bytes=20_000)[0]

    assert len(whole.payloads) == 1 and len(split.payloads) > 1
    assert all(len(json.dumps(payload).encode()) <= 20_000 for payload in split.payloads)
    ids = [span["spanId"] for span in spans_of(split)]
    assert len(ids) == len(set(ids)) == len(spans_of(whole))
    assert {span["spanId"] for span in spans_of(whole)} == set(ids)


def test_an_item_trace_survives_a_real_http_round_trip(tmp_path: Path) -> None:
    trace = build(tmp_path, [Scenario("books-1")])[0]
    fake = FakeLangfuse()

    with fake.serve() as url, LangfuseClient(settings(host=url)) as client:
        for payload in trace.payloads:
            client.export_otlp(payload)
        assert client.trace_exists(trace.trace_id)

    request = fake.requests[0]
    assert request.headers["x-langfuse-ingestion-version"] == "4"
    assert request.headers["content-type"] == "application/json"
    span = fake.spans()[0]
    assert span["startTimeUnixNano"].isdigit() and len(span["traceId"]) == 32 and len(span["spanId"]) == 16
