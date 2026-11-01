"""Neutral records for an evaluation platform: dataset items and scores (pure, no I/O).

A Golden Set scenario becomes a :class:`DatasetItemRecord`; a finished batch (``batch.json``,
the ``run_index.jsonl`` rows and the local verdict of
:func:`~src.agent_loop.batch_compare.compare_batches`) becomes :class:`ScoreRecord` values.
Score names and categories come from the code's own vocabularies (``FailureKind``,
``StopReason``, ``CheckStatus``, ``BatchFindingKind``), so the platform never holds a list of
its own. A platform bridge outside ``src/`` maps these records to its API; nothing here knows
a vendor. The platform is only a second view: the verdict is computed locally and sent as
scores, never recomputed there. See ADR-2026-10-31 in docs/decisions/.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from src.agent_loop.batch import load_batch_scenarios
from src.agent_loop.batch_compare import BatchComparison, BatchFindingKind
from src.agent_loop.failures import INFRA_KINDS, FailureKind
from src.agent_loop.graders import GRADED_KEYS, CheckStatus
from src.contracts import StopReason

ScoreDataType = Literal["numeric", "categorical", "boolean", "text"]
ScoreTarget = Literal["item", "task", "run"]
Verdict = Literal["no_regression", "regression", "incomparable", "no_baseline"]

#: ``row["metrics"]`` figures sent as item scores (duration, tokens and cost the platform
#: derives from the spans itself).
METRIC_SCORES = ("model_calls", "tool_calls", "tool_errors", "policy_blocks", "time_to_first_tool_ms")
#: Check keys a batch writes itself: the run failed, or ``expected`` was not a mapping.
_BATCH_CHECKS = ("run", "expected")
#: Finding kinds that describe a scenario of the newer run; ``removed`` names one it no longer
#: has, ``slower``/``costlier`` are metric notes on a scenario passed in both.
_ITEM_FINDINGS = tuple(
    kind for kind in get_args(BatchFindingKind) if kind not in {"removed", "slower", "costlier"}
)
_UNCHANGED = "unchanged"
#: Kinds that are not a failure of the agent (the report's "Passed" side).
_NOT_FAILED = frozenset({"passed", "ungraded"})


@dataclass(frozen=True)
class DatasetItemRecord:
    """One scenario as a dataset item."""

    #: Stable across content changes: ``f"{dataset}\\n{scenario_id}"``.
    item_key: str
    scenario_id: str
    #: ``{"tasks": [...], "scenario_id": ...}``: repository data.
    input: dict[str, Any]
    #: ``{"checks": <expected as is>, "reference": <str | None>}``; ``None`` without either.
    expected: dict[str, Any] | None
    #: ``scenario_id``, ``tags``, ``suite``, ``content_sha256``, ``source_path``.
    metadata: dict[str, Any]
    #: Hash of the scenario's canonical JSON: changes exactly when the scenario does.
    content_sha256: str


@dataclass(frozen=True)
class ScoreDefinition:
    """A score's name, type and allowed values (the platform's score configuration)."""

    name: str
    data_type: ScoreDataType
    categories: tuple[str, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


@dataclass(frozen=True)
class ScoreRecord:
    """One score of a batch: on a scenario (``item``), one of its tasks, or the whole run."""

    name: str
    data_type: ScoreDataType
    value: float | str | bool
    target: ScoreTarget
    scenario_id: str | None = None
    task_id: str | None = None
    comment: str = ""


class DatasetSource(Protocol):
    """A scenario set that can be pushed as a dataset (the Golden Set; later the live set)."""

    suite: str

    def load(self, path: Path) -> list[DatasetItemRecord]: ...


def platform_key(*parts: str, length: int = 32) -> str:
    """A deterministic hex id: the sha256 of the parts joined by newlines, truncated."""

    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:length]


def golden_items(path: Path, *, dataset: str, suite: str, source_path: str) -> list[DatasetItemRecord]:
    """The scenarios of a Golden Set JSONL file as dataset items of ``dataset``."""

    items: list[DatasetItemRecord] = []
    for scenario in load_batch_scenarios(path):
        content = {
            "scenario_id": scenario.scenario_id,
            "tasks": list(scenario.tasks),
            "expected": scenario.expected,
            "tags": list(scenario.tags or []),
            "reference": scenario.reference,
        }
        content_sha256 = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        expected = (
            None
            if scenario.expected is None and scenario.reference is None
            else {"checks": scenario.expected, "reference": scenario.reference}
        )
        items.append(
            DatasetItemRecord(
                item_key=f"{dataset}\n{scenario.scenario_id}",
                scenario_id=scenario.scenario_id,
                input={"tasks": list(scenario.tasks), "scenario_id": scenario.scenario_id},
                expected=expected,
                metadata={
                    "scenario_id": scenario.scenario_id,
                    "tags": list(scenario.tags or []),
                    "suite": suite,
                    "content_sha256": content_sha256,
                    "source_path": source_path,
                },
                content_sha256=content_sha256,
            )
        )
    return items


def score_definitions() -> list[ScoreDefinition]:
    """Every score a batch can send, with categories taken from the code's ``Literal`` types."""

    kinds = get_args(FailureKind)
    checks = get_args(CheckStatus)
    definitions = [
        ScoreDefinition("passed", "boolean", description="The scenario passed (failure_kind passed)."),
        ScoreDefinition("failure_kind", "categorical", kinds, description="How the scenario ended."),
        ScoreDefinition("infra_failure", "boolean", description="The failure was infrastructure, not the agent: rerun."),
        ScoreDefinition("grade", "categorical", checks, description="Grade of the last answer against expected."),
        *(
            ScoreDefinition(f"check.{key}", "categorical", checks, description=f"The {key!r} check of the grade.")
            for key in (*GRADED_KEYS, *_BATCH_CHECKS)
        ),
        ScoreDefinition(
            "regression",
            "categorical",
            (*_ITEM_FINDINGS, _UNCHANGED),
            description="The scenario against the previous comparable run (local verdict).",
        ),
        *(
            ScoreDefinition(name, "numeric", min_value=0.0, description=f"{name} of the scenario run.")
            for name in METRIC_SCORES
        ),
        ScoreDefinition("stop_reason", "categorical", get_args(StopReason), description="Why the task stopped."),
        ScoreDefinition("task_failure_kind", "categorical", kinds, description="How the task ended."),
        ScoreDefinition("pass_rate", "numeric", min_value=0.0, max_value=1.0, description="Share of passed scenarios."),
        ScoreDefinition("agent_failures", "numeric", min_value=0.0, description="Scenarios failed by the agent."),
        ScoreDefinition("infra_failures", "numeric", min_value=0.0, description="Scenarios failed by infrastructure."),
        ScoreDefinition(
            "verdict",
            "categorical",
            get_args(Verdict),
            description="The local verdict against the previous comparable run.",
        ),
        ScoreDefinition("regressions", "numeric", min_value=0.0, description="Regression findings of the run."),
    ]
    return definitions


def batch_score_records(
    metadata: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    comparison: BatchComparison | None,
) -> list[ScoreRecord]:
    """The scores of a finished batch: per scenario, per task and for the run.

    ``comparison`` is the local verdict against the previous comparable run (``None`` when
    there is none). Grade and check scores exist only for a graded batch; checks without a
    grader (``ungraded``) get none — a judge scores those.
    """

    _ = metadata  # the batch identity lives in the bridge's ids; kept for the contract
    findings: dict[str, str] = {}
    usable = comparison is not None and comparison.comparable
    if usable:
        for finding in comparison.findings:  # type: ignore[union-attr]
            if finding.kind in _ITEM_FINDINGS:
                findings.setdefault(finding.scenario_id, finding.kind)

    records: list[ScoreRecord] = []
    for row in rows:
        records += _item_records(row, findings if usable else None)
        records += _task_records(row)
    records += _run_records(rows, comparison)
    return records


def _item_records(row: Mapping[str, Any], findings: Mapping[str, str] | None) -> list[ScoreRecord]:
    scenario = str(row.get("scenario_id") or "")
    kind = str(row.get("failure_kind") or "error")

    def item(name: str, data_type: ScoreDataType, value: float | str | bool, comment: str = "") -> ScoreRecord:
        return ScoreRecord(name, data_type, value, "item", scenario_id=scenario, comment=comment)

    records = [
        item("passed", "boolean", kind == "passed"),
        item("failure_kind", "categorical", kind),
        item("infra_failure", "boolean", kind in INFRA_KINDS),
    ]
    grade = row.get("grade")
    if isinstance(grade, Mapping) and grade.get("status"):
        records.append(item("grade", "categorical", str(grade["status"])))
        for check in grade.get("checks") or []:
            status = str(check.get("status") or "")
            if status and status != "ungraded":
                records.append(
                    item(f"check.{check.get('key')}", "categorical", status, str(check.get("message") or ""))
                )
    if findings is not None:
        records.append(item("regression", "categorical", findings.get(scenario, _UNCHANGED)))
    metrics = row.get("metrics") or {}
    for name in METRIC_SCORES:
        value = metrics.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            records.append(item(name, "numeric", float(value)))
    return records


def _task_records(row: Mapping[str, Any]) -> list[ScoreRecord]:
    scenario = str(row.get("scenario_id") or "")
    records: list[ScoreRecord] = []
    for outcome in row.get("task_outcomes") or []:
        task_id = str(outcome.get("task_id") or "")
        if not task_id:
            continue
        records.append(
            ScoreRecord(
                "task_failure_kind",
                "categorical",
                str(outcome.get("failure_kind") or "error"),
                "task",
                scenario_id=scenario,
                task_id=task_id,
            )
        )
        stop_reason = str(outcome.get("stop_reason") or "")
        if stop_reason:
            records.append(
                ScoreRecord("stop_reason", "categorical", stop_reason, "task", scenario_id=scenario, task_id=task_id)
            )
    return records


def _run_records(rows: Sequence[Mapping[str, Any]], comparison: BatchComparison | None) -> list[ScoreRecord]:
    kinds = [str(row.get("failure_kind") or "error") for row in rows]
    passed = sum(1 for kind in kinds if kind == "passed")
    infra = sum(1 for kind in kinds if kind in INFRA_KINDS)
    agent = sum(1 for kind in kinds if kind not in INFRA_KINDS | _NOT_FAILED)
    records = [
        ScoreRecord("pass_rate", "numeric", passed / len(kinds) if kinds else 0.0, "run"),
        ScoreRecord("agent_failures", "numeric", float(agent), "run"),
        ScoreRecord("infra_failures", "numeric", float(infra), "run"),
    ]
    verdict: Verdict
    if comparison is None:
        verdict = "no_baseline"
    elif not comparison.comparable:
        verdict = "incomparable"
    else:
        regressions = sum(1 for finding in comparison.findings if finding.kind == "regression")
        verdict = "regression" if regressions else "no_regression"
        records.append(ScoreRecord("regressions", "numeric", float(regressions), "run"))
    records.append(ScoreRecord("verdict", "categorical", verdict, "run"))
    return records
