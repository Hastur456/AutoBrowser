"""Scores of a synced batch, and their Langfuse score configs (§5.4.1, §5.4.2).

The records come from ``src/agent_loop/platform_records.batch_score_records`` (grade, checks,
failure kinds, stop reasons, metrics and the local verdict of ``compare_batches`` against the
previous comparable batch); this module maps them to Langfuse: an item score on the item
trace's root observation, a task score on the task's span, a run score on the experiment
(``datasetRunId``). Ids are ``sha256(experiment + target + name)`` and the timestamp is the
batch's ``finished_at``, so sending again upserts instead of duplicating. A 207 is recorded,
never retried automatically (Langfuse's rule): ``--retry-rejected`` sends the rejected ones
again, typically after ``setup`` fixed a config.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.langfuse_bridge.client import LangfuseClient
from scripts.langfuse_bridge.experiments import (
    BATCH_FILE_NAME,
    RUN_INDEX_FILE_NAME,
    BatchFolder,
    span_id,
)
from src.agent_loop.batch_compare import BatchComparison, compare_batches
from src.agent_loop.benchmarks import split_by_task
from src.agent_loop.events import read_jsonl_lines
from src.agent_loop.platform_records import (
    ScoreDefinition,
    ScoreRecord,
    batch_score_records,
    platform_key,
)
from src.agent_loop.replay import load_events
from src.config import PipelineSettings

_DATA_TYPES = {"numeric": "NUMERIC", "categorical": "CATEGORICAL", "boolean": "BOOLEAN", "text": "TEXT"}


def config_body(definition: ScoreDefinition) -> dict[str, Any]:
    body: dict[str, Any] = {"name": definition.name, "dataType": _DATA_TYPES[definition.data_type]}
    if definition.categories:
        body["categories"] = [{"label": label, "value": index} for index, label in enumerate(definition.categories)]
    if definition.min_value is not None:
        body["minValue"] = definition.min_value
    if definition.max_value is not None:
        body["maxValue"] = definition.max_value
    if definition.description:
        body["description"] = definition.description
    return body


def ensure_score_configs(client: LangfuseClient, definitions: Sequence[ScoreDefinition]) -> dict[str, str]:
    """Config id by score name. Configs are immutable: a changed definition (a grown
    vocabulary) archives the old config and creates a new one under the same name."""

    active = {config["name"]: config for config in client.score_configs() if not config.get("isArchived")}
    ids: dict[str, str] = {}
    for definition in definitions:
        wanted = config_body(definition)
        current = active.get(definition.name)
        if current is not None and _same(current, wanted):
            ids[definition.name] = str(current["id"])
            continue
        if current is not None:
            client.archive_score_config(str(current["id"]))
        ids[definition.name] = client.create_score_config(wanted)
    return ids


def local_comparison(folder: BatchFolder, baseline_batch_id: str | None, pipeline: PipelineSettings) -> BatchComparison | None:
    """The local verdict of this batch against its baseline, as ``compare_batches.py`` gives it."""

    if not baseline_batch_id:
        return None
    baseline = folder.path.parent / baseline_batch_id
    if not (baseline / BATCH_FILE_NAME).is_file():
        return None
    meta = json.loads((baseline / BATCH_FILE_NAME).read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in read_jsonl_lines(baseline / RUN_INDEX_FILE_NAME)]
    return compare_batches(meta, rows, folder.metadata, folder.rows, pipeline)


def score_bodies(
    folder: BatchFolder,
    records: Sequence[ScoreRecord],
    *,
    experiment: str,
    items: Mapping[str, Mapping[str, Any]],
    config_ids: Mapping[str, str],
    environment: str,
) -> list[dict[str, Any]]:
    """Langfuse score bodies; a record whose trace or task span is unknown is skipped."""

    task_spans = _task_spans(folder, items)
    timestamp = str(folder.metadata.get("finished_at"))
    bodies: list[dict[str, Any]] = []
    for record in records:
        body: dict[str, Any] = {
            "name": record.name,
            "value": _value(record),
            "dataType": _DATA_TYPES[record.data_type],
            "environment": environment,
            "timestamp": timestamp,
        }
        if record.target == "item":
            item = items.get(str(record.scenario_id))
            if not item:
                continue
            target = f"item:{record.scenario_id}"
            body.update(traceId=item["trace_id"], observationId=item["root_span_id"])
        elif record.target == "task":
            span = task_spans.get(str(record.task_id))
            if span is None:
                continue
            target = f"task:{record.task_id}"
            body.update(traceId=span[0], observationId=span[1])
        else:
            target = "run"
            body["datasetRunId"] = experiment
        body["id"] = platform_key(experiment, target, record.name)
        if record.comment:
            body["comment"] = record.comment
        if record.name in config_ids:
            body["configId"] = config_ids[record.name]
        bodies.append(body)
    return bodies


def batch_records(folder: BatchFolder, comparison: BatchComparison | None) -> list[ScoreRecord]:
    return batch_score_records(folder.metadata, folder.rows, comparison)


def _task_spans(folder: BatchFolder, items: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """Task id -> (trace id, span id of its ``invoke_agent`` span), from the batch's traces."""

    spans: dict[str, tuple[str, str]] = {}
    for row in folder.rows:
        item = items.get(str(row.get("scenario_id")))
        trace = row.get("trace_path")
        path = folder.path / str(trace) if trace else None
        if not item or path is None or not Path(path).is_file():
            continue
        for task in split_by_task(load_events(path)):
            start = next((event for event in task if event.type == "goal.started"), None)
            if start is not None and start.task_id:
                spans[start.task_id] = (item["trace_id"], span_id(item["trace_id"], start.event_id))
    return spans


def _value(record: ScoreRecord) -> float | str:
    if record.data_type == "boolean":
        return 1 if record.value else 0
    if record.data_type == "numeric":
        return float(record.value)
    return str(record.value)


def _same(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    if current.get("dataType") != wanted["dataType"]:
        return False
    labels = [category.get("label") for category in current.get("categories") or []]
    if labels != [category["label"] for category in wanted.get("categories") or []]:
        return False
    return current.get("minValue") == wanted.get("minValue") and current.get("maxValue") == wanted.get("maxValue")
