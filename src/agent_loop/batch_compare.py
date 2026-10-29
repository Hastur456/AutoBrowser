"""The verdict between two batch runs (pure: metadata and rows in, findings out).

Two runs are comparable when they ran the same Golden Set (``provenance.tasks_sha256``); other
differences (settings, model) are warnings. Per scenario present in both, the change of its
``failure_kind`` is a finding: ``passed`` to an agent kind is a ``regression``, the reverse is
``fixed``, anything to an infra kind is ``inconclusive`` (rerun it). For scenarios passed in
both runs, a relative growth of duration, model calls or input tokens past
``pipeline.batch_metric_tolerance`` is ``slower``/``costlier`` — a single live run is noisy, so
these fail only under the perf gate. ``scripts/compare_batches.py`` loads the files and prints.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.agent_loop.failures import INFRA_KINDS
from src.config import PipelineSettings

BatchFindingKind = Literal[
    "regression",
    "fixed",
    "inconclusive",
    "changed",
    "added",
    "removed",
    "slower",
    "costlier",
]
#: Kinds that are not a failure of the agent: neither a pass nor its fault.
_NEUTRAL_KINDS = INFRA_KINDS | {"passed", "ungraded"}
#: Compared metric -> finding kind when it grows past the tolerance.
_METRIC_FINDINGS: dict[str, BatchFindingKind] = {
    "duration_ms": "slower",
    "model_calls": "costlier",
    "input_tokens": "costlier",
}
_PERF_KINDS = frozenset(_METRIC_FINDINGS.values())


@dataclass(frozen=True)
class BatchFinding:
    """One difference between the two runs for one scenario."""

    kind: BatchFindingKind
    scenario_id: str
    #: The failure kinds before and after, or the metric values for a perf finding.
    before: Any = None
    after: Any = None
    #: The compared metric of a perf finding, ``""`` otherwise.
    metric: str = ""
    #: Relative change of a perf finding (``0.3`` is +30 %).
    change: float | None = None

    def describe(self) -> str:
        if self.metric:
            return (
                f"{self.kind}: {self.scenario_id} {self.metric} {self.before} -> {self.after}"
                f" ({self.change:+.0%})"
            )
        if self.kind in {"added", "removed"}:
            value = self.after if self.kind == "added" else self.before
            return f"{self.kind}: {self.scenario_id} ({value})"
        return f"{self.kind}: {self.scenario_id} {self.before} -> {self.after}"


@dataclass(frozen=True)
class BatchComparison:
    """Findings of run ``batch_b`` (the newer) against run ``batch_a``."""

    batch_a: str
    batch_b: str
    comparable: bool
    warnings: list[str] = field(default_factory=list)
    findings: list[BatchFinding] = field(default_factory=list)
    #: Scenarios present in both runs with nothing to report.
    unchanged: int = 0

    def exit_code(self, perf_gate: bool = False) -> int:
        """``2`` incomparable, ``1`` a regression, ``3`` perf findings under the gate, else ``0``."""

        if not self.comparable:
            return 2
        kinds = {finding.kind for finding in self.findings}
        if "regression" in kinds:
            return 1
        if perf_gate and kinds & _PERF_KINDS:
            return 3
        return 0

    def to_json_dict(self, perf_gate: bool = False) -> dict[str, Any]:
        return {
            "batch_a": self.batch_a,
            "batch_b": self.batch_b,
            "comparable": self.comparable,
            "warnings": list(self.warnings),
            "findings": [asdict(finding) for finding in self.findings],
            "unchanged": self.unchanged,
            "exit_code": self.exit_code(perf_gate),
        }

    def render_markdown(self) -> str:
        lines = [
            "# Batch comparison",
            "",
            f"- Before: {self.batch_a}",
            f"- After: {self.batch_b}",
            f"- Comparable: {'yes' if self.comparable else 'no (different Golden Sets)'}",
            f"- Unchanged scenarios: {self.unchanged}",
        ]
        lines += [f"- Warning: {warning}" for warning in self.warnings]
        if self.findings:
            lines += [
                "",
                "| Finding | Scenario | Before | After | Metric | Change |",
                "|---|---|---|---|---|---:|",
            ]
            for finding in self.findings:
                change = "" if finding.change is None else f"{finding.change:+.0%}"
                lines.append(
                    f"| {finding.kind} | {finding.scenario_id} | {_cell(finding.before)}"
                    f" | {_cell(finding.after)} | {finding.metric} | {change} |"
                )
        return "\n".join(lines) + "\n"


def compare_batches(
    meta_a: Mapping[str, Any],
    rows_a: Sequence[Mapping[str, Any]],
    meta_b: Mapping[str, Any],
    rows_b: Sequence[Mapping[str, Any]],
    settings: PipelineSettings,
) -> BatchComparison:
    """Compare run ``b`` (the newer) with run ``a``; never raises for a difference."""

    batch_a = str(meta_a.get("batch_id") or "")
    batch_b = str(meta_b.get("batch_id") or "")
    prov_a = meta_a.get("provenance") or {}
    prov_b = meta_b.get("provenance") or {}
    tasks_a, tasks_b = prov_a.get("tasks_sha256"), prov_b.get("tasks_sha256")
    if not tasks_a or tasks_a != tasks_b:
        return BatchComparison(batch_a, batch_b, comparable=False)

    warnings: list[str] = []
    if prov_a.get("settings_sha256") != prov_b.get("settings_sha256"):
        warnings.append(
            f"settings differ ({_short(prov_a.get('settings_sha256'))} ->"
            f" {_short(prov_b.get('settings_sha256'))})"
        )
    if prov_a.get("model") != prov_b.get("model"):
        warnings.append(f"model differs ({prov_a.get('model')} -> {prov_b.get('model')})")

    before = _by_scenario(rows_a)
    after = _by_scenario(rows_b)
    findings: list[BatchFinding] = []
    unchanged = 0
    for scenario_id, row_b in after.items():
        row_a = before.get(scenario_id)
        if row_a is None:
            findings.append(BatchFinding("added", scenario_id, after=row_b.get("failure_kind")))
            continue
        scenario_findings = _scenario_findings(scenario_id, row_a, row_b, settings)
        findings += scenario_findings
        unchanged += not scenario_findings
    findings += [
        BatchFinding("removed", scenario_id, before=row_a.get("failure_kind"))
        for scenario_id, row_a in before.items()
        if scenario_id not in after
    ]
    return BatchComparison(batch_a, batch_b, True, warnings, findings, unchanged)


def _scenario_findings(
    scenario_id: str,
    row_a: Mapping[str, Any],
    row_b: Mapping[str, Any],
    settings: PipelineSettings,
) -> list[BatchFinding]:
    kind_a, kind_b = row_a.get("failure_kind"), row_b.get("failure_kind")
    if kind_a != kind_b:
        return [BatchFinding(_kind_change(kind_a, kind_b), scenario_id, kind_a, kind_b)]
    if kind_a != "passed":
        return []
    metrics_a = row_a.get("metrics") or {}
    metrics_b = row_b.get("metrics") or {}
    findings: list[BatchFinding] = []
    for metric, kind in _METRIC_FINDINGS.items():
        value_a, value_b = metrics_a.get(metric), metrics_b.get(metric)
        if not _positive_number(value_a) or not _positive_number(value_b, allow_zero=True):
            continue
        change = (value_b - value_a) / value_a
        if change > settings.batch_metric_tolerance:
            findings.append(BatchFinding(kind, scenario_id, value_a, value_b, metric, change))
    return findings


def _kind_change(kind_a: Any, kind_b: Any) -> BatchFindingKind:
    if kind_b in INFRA_KINDS:
        return "inconclusive"
    agent_a = kind_a is not None and kind_a not in _NEUTRAL_KINDS
    agent_b = kind_b is not None and kind_b not in _NEUTRAL_KINDS
    if kind_a == "passed" and agent_b:
        return "regression"
    if agent_a and kind_b == "passed":
        return "fixed"
    return "changed"


def _by_scenario(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(row.get("scenario_id")): row for row in rows}


def _positive_number(value: Any, *, allow_zero: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return value >= 0 if allow_zero else value > 0


def _short(digest: Any) -> str:
    return str(digest)[:12] if digest else "none"


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


__all__ = [
    "BatchComparison",
    "BatchFinding",
    "BatchFindingKind",
    "compare_batches",
]
