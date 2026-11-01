"""A finished batch as a Langfuse experiment: one OTLP/JSON item trace per scenario (§5.3).

The trace is built after the run from the batch folder, with the events' own timestamps: a
root ``scenario {scenario_id}`` span (the dataset item's observation) over the task trees of
``src/harness/otel_spans.task_span_specs``. Trace and span ids are deterministic (experiment
id + scenario id), so the same batch always gives the same trace. Every span carries the
``langfuse.experiment.*`` keys and the trace-level attributes (Langfuse v4 reads them per
span). What leaves the machine besides repository data is ``eval_platform.capture_content``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.langfuse_bridge.datasets import item_id
from src.agent_loop.benchmarks import split_by_task
from src.agent_loop.events import EventRecord, read_jsonl_lines, redact_json_safe
from src.agent_loop.failures import INFRA_KINDS
from src.agent_loop.platform_records import (
    DatasetItemRecord,
    golden_items,
    platform_key,
)
from src.agent_loop.replay import load_events
from src.config import EvalPlatformSettings
from src.harness.otel_spans import (
    AttributeValue,
    SpanContext,
    SpanSpec,
    encode_otlp_json,
    task_span_specs,
)

BATCH_FILE_NAME = "batch.json"
RUN_INDEX_FILE_NAME = "run_index.jsonl"
TASKS_COPY_FILE_NAME = "tasks.jsonl"
ROOT_KEY = "scenario"
EXPERIMENT = "langfuse.experiment"
_NOT_FAILED = frozenset({"passed", "ungraded"})
_NAME_SEPARATOR = " · "
_SHORT_COMMIT = 7
_SHORT_BATCH = len("batch-") + 6


@dataclass(frozen=True)
class BatchFolder:
    path: Path
    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    #: The dataset items of the batch's own copy of the scenario set, by scenario id.
    items: dict[str, DatasetItemRecord]


@dataclass(frozen=True)
class ExperimentRef:
    id: str
    name: str
    dataset_name: str
    dataset_id: str
    #: Langfuse's dataset version the batch ran on, when the journal maps its content.
    item_version: str | None
    version_matched: bool
    baseline_batch_id: str | None


@dataclass(frozen=True)
class ItemTrace:
    scenario_id: str
    trace_id: str
    root_span_id: str
    #: OTLP/JSON request bodies; more than one only for a trace past ``max_request_bytes``.
    payloads: list[dict[str, Any]]


def load_batch_folder(path: Path, *, dataset: str, suite: str) -> BatchFolder:
    path = Path(path)
    metadata = json.loads((path / BATCH_FILE_NAME).read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in read_jsonl_lines(path / RUN_INDEX_FILE_NAME)]
    copy = path / TASKS_COPY_FILE_NAME
    records = golden_items(copy, dataset=dataset, suite=suite, source_path=TASKS_COPY_FILE_NAME) if copy.is_file() else []
    return BatchFolder(path, metadata, rows, {record.scenario_id: record for record in records})


def experiment_id(batch_id: str, attempt: int) -> str:
    """The batch id; a later attempt (a new project, a manual reset) gets a suffix."""

    return batch_id if attempt <= 1 else f"{batch_id}.{attempt}"


def experiment_name(metadata: Mapping[str, Any], suite: str) -> str:
    provenance = metadata.get("provenance") or {}
    git = provenance.get("git") or {}
    started = str(metadata.get("started_at") or "")
    try:
        when = datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        when = started
    commit = str(git.get("commit") or "nogit")[:_SHORT_COMMIT] + ("*" if git.get("dirty") else "")
    parts = [suite, when, commit, str(provenance.get("model") or "unknown"), str(metadata.get("batch_id") or "")[:_SHORT_BATCH]]
    return _NAME_SEPARATOR.join(parts)


def experiment_description(metadata: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    provenance = metadata.get("provenance") or {}
    git = provenance.get("git") or {}
    kinds = [str(row.get("failure_kind") or "error") for row in rows]
    agent = sum(1 for kind in kinds if kind not in INFRA_KINDS | _NOT_FAILED)
    infra = sum(1 for kind in kinds if kind in INFRA_KINDS)
    passed = sum(1 for kind in kinds if kind in _NOT_FAILED)
    commit = str(git.get("commit") or "nogit")[:_SHORT_COMMIT] + (" (dirty)" if git.get("dirty") else "")
    return (
        f"Commit {commit} · model {provenance.get('model')} ({provenance.get('provider')}) · "
        f"{len(rows)} scenarios. Agent failures: {agent} · Infra failures: {infra} · Passed: {passed}"
    )


def experiment_metadata(metadata: Mapping[str, Any], ref: ExperimentRef) -> dict[str, str]:
    provenance = metadata.get("provenance") or {}
    git = provenance.get("git") or {}
    values: dict[str, Any] = {
        "batch_id": metadata.get("batch_id"),
        "git_commit": git.get("commit"),
        "git_branch": git.get("branch"),
        "git_dirty": git.get("dirty"),
        "model": provenance.get("model"),
        "provider": provenance.get("provider"),
        "tasks_sha256": provenance.get("tasks_sha256"),
        "settings_sha256": provenance.get("settings_sha256"),
        "non_default_sections": ",".join(provenance.get("non_default_sections") or []),
        "python": provenance.get("python"),
        "platform": provenance.get("platform"),
        "dataset_version": ref.item_version,
        "dataset_version_matched": ref.version_matched,
        "baseline_batch_id": ref.baseline_batch_id,
        "graded": (metadata.get("config") or {}).get("grade"),
        "cli_options": json.dumps(metadata.get("config") or {}, sort_keys=True, ensure_ascii=False),
    }
    return {key: _string(value) for key, value in values.items() if value is not None}


def item_trace_id(experiment: str, scenario_id: str) -> str:
    return platform_key(experiment, scenario_id)


def span_id(trace_id: str, key: str) -> str:
    return platform_key(trace_id, key, length=16)


def item_trace(
    folder: BatchFolder,
    row: Mapping[str, Any],
    ref: ExperimentRef,
    settings: EvalPlatformSettings,
    *,
    service_name: str,
) -> ItemTrace:
    """The scenario of ``row`` as one item trace, split into requests only when too large."""

    scenario = str(row.get("scenario_id") or "")
    trace_id = item_trace_id(ref.id, scenario)
    root_span_id = span_id(trace_id, ROOT_KEY)
    capture = settings.capture_content
    metadata = folder.metadata
    provenance = metadata.get("provenance") or {}
    events = _events(folder, row)

    children: list[SpanSpec] = []
    task_context = SpanContext(
        model=provenance.get("model"),
        provider=provenance.get("provider"),
    )
    for task in split_by_task(events):
        for spec in task_span_specs(task, capture_content=capture, context=task_context):
            children.append(replace(spec, parent_key=spec.parent_key or ROOT_KEY))
    if events:
        start_ns, end_ns = _ns(events[0].timestamp), _ns(events[-1].timestamp)
    else:
        start_ns = _ns(datetime.fromisoformat(str(row["started_at"])))
        end_ns = _ns(datetime.fromisoformat(str(row["finished_at"])))
    if children:
        start_ns = min(start_ns, *(spec.start_ns for spec in children))
        end_ns = max(end_ns, *(spec.end_ns for spec in children))

    record = folder.items.get(scenario)
    kind = str(row.get("failure_kind") or "error")
    root_attributes: dict[str, AttributeValue] = {
        "langfuse.observation.type": "agent",
        "langfuse.observation.input": _json(
            record.input if record else {"tasks": row.get("tasks") or [], "scenario_id": scenario}
        ),
        "langfuse.observation.output": _output(row, capture),
        f"{EXPERIMENT}.description": experiment_description(metadata, folder.rows),
        f"{EXPERIMENT}.item.metadata.scenario_id": scenario,
    }
    if record is not None:
        if record.expected is not None:
            root_attributes[f"{EXPERIMENT}.item.expected_output"] = _json(record.expected)
        root_attributes[f"{EXPERIMENT}.item.metadata.tags"] = _json(record.metadata.get("tags") or [])
        root_attributes[f"{EXPERIMENT}.item.metadata.content_sha256"] = record.content_sha256
    if ref.item_version and ref.version_matched:
        root_attributes[f"{EXPERIMENT}.item.version"] = ref.item_version
    for key, value in experiment_metadata(metadata, ref).items():
        root_attributes[f"{EXPERIMENT}.metadata.{key}"] = value
    root = SpanSpec(
        key=ROOT_KEY,
        parent_key=None,
        name=f"scenario {scenario}",
        start_ns=start_ns,
        end_ns=end_ns,
        attributes=root_attributes,
        error=None if kind in _NOT_FAILED else kind,
    )

    common = _common_attributes(folder, row, ref, root_span_id, settings)
    specs = [replace(spec, attributes={**spec.attributes, **common}) for spec in [root, *children]]
    payloads = _split(specs, trace_id, service_name, settings.max_request_bytes)
    return ItemTrace(scenario_id=scenario, trace_id=trace_id, root_span_id=root_span_id, payloads=payloads)


def _common_attributes(
    folder: BatchFolder,
    row: Mapping[str, Any],
    ref: ExperimentRef,
    root_span_id: str,
    settings: EvalPlatformSettings,
) -> dict[str, AttributeValue]:
    scenario = str(row.get("scenario_id") or "")
    commit = ((folder.metadata.get("provenance") or {}).get("git") or {}).get("commit")
    common: dict[str, AttributeValue] = {
        f"{EXPERIMENT}.id": ref.id,
        f"{EXPERIMENT}.name": ref.name,
        f"{EXPERIMENT}.dataset.id": ref.dataset_id,
        f"{EXPERIMENT}.item.id": item_id(ref.dataset_name, scenario),
        f"{EXPERIMENT}.item.root_observation_id": root_span_id,
        "langfuse.environment": settings.environment,
        "autobrowser.batch.id": str(folder.metadata.get("batch_id") or ""),
        "autobrowser.scenario.id": scenario,
    }
    if commit:
        common["vcs.ref.head.revision"] = str(commit)
    if row.get("session_id"):
        common["session.id"] = str(row["session_id"])
    return common


def _output(row: Mapping[str, Any], capture: str) -> str:
    """The item's output: the last answer (opt-in), else the outcome without any answer text."""

    answer = row.get("final_answer")
    if capture in {"answers", "redacted"} and isinstance(answer, str) and answer:
        return str(redact_json_safe(answer))
    grade = row.get("grade")
    outcome: dict[str, Any] = {
        "failure_kind": row.get("failure_kind"),
        "grade": grade.get("status") if isinstance(grade, Mapping) else None,
        "task_statuses": list(row.get("task_statuses") or []),
    }
    error = row.get("error")
    if isinstance(error, Mapping) and error.get("type"):
        outcome["error"] = {"type": str(error["type"])}
    return _json(outcome)


def _events(folder: BatchFolder, row: Mapping[str, Any]) -> list[EventRecord]:
    trace = row.get("trace_path")
    path = folder.path / str(trace) if trace else None
    return load_events(path) if path is not None and path.is_file() else []


def _split(specs: list[SpanSpec], trace_id: str, service_name: str, limit: int) -> list[dict[str, Any]]:
    """OTLP/JSON bodies of at most ``limit`` bytes (a lone oversize span goes alone); each
    span appears in exactly one body."""

    def encode(group: list[SpanSpec]) -> dict[str, Any]:
        return encode_otlp_json(
            group,
            resource={"service.name": service_name},
            trace_id=trace_id,
            span_id_of=lambda key: span_id(trace_id, key),
        )

    whole = encode(specs)
    if _size(whole) <= limit:
        return [whole]
    payloads: list[dict[str, Any]] = []
    group: list[SpanSpec] = []
    for spec in specs:
        if group and _size(encode([*group, spec])) > limit:
            payloads.append(encode(group))
            group = []
        group.append(spec)
    if group:
        payloads.append(encode(group))
    return payloads


def _size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _ns(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
