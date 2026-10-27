"""Compare eval metrics with a committed baseline and report findings.

A baseline (schema v2, ``tests/evals/baselines/agent_loop_v2.json``) holds one metric record per
scenario, plus an optional ``scenarios_with_memory`` block for the runs with a read-only memory
seed. :func:`compare` checks a run against one block under a set of :class:`MetricRule` s and
returns a :class:`ComparisonReport`; it never raises for a mismatch.

The rules are fixed vocabulary — what each metric means — not tuning: the only tolerance comes
from :class:`~src.config.PipelineSettings`. In the deterministic suites every counter must match
exactly, and the report says which way it moved so a reviewer can decide whether to update the
baseline. ``request_chars`` is the exception: a continuous size with a relative tolerance.

See docs/development/2026-10-19-autonomous-test-pipeline-design.md §4.3.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.config import PipelineSettings

BASELINE_SCHEMA_VERSION = 2
#: Baseline block of the plain runs and of the runs with a memory seed.
SCENARIOS_BLOCK = "scenarios"
MEMORY_BLOCK = "scenarios_with_memory"

#: ``exact`` — any difference is a finding; ``ratio`` — a relative change past the tolerance.
MetricKind = Literal["exact", "ratio"]
FindingKind = Literal[
    "regression",
    "improvement",
    "changed",
    "script_exhausted",
    "missing_baseline",
    "orphan_baseline",
]
#: Finding kinds that fail a comparison; ``improvement`` is reported only.
FAILING_KINDS: frozenset[str] = frozenset(
    {"regression", "changed", "script_exhausted", "missing_baseline", "orphan_baseline"}
)

#: Counters where fewer is probably better (direction hint in a ``changed`` finding).
_FEWER_IS_BETTER = frozenset(
    {
        "model_turn_count",
        "tool_call_count",
        "policy_block_count",
        "repeated_action_count",
        "approval_request_count",
        "error_count",
        "observation_count",
    }
)


class BaselineError(ValueError):
    """A baseline file is missing, unreadable or not a v2 baseline."""


@dataclass(frozen=True)
class MetricRule:
    """How one metric is compared."""

    metric: str
    kind: MetricKind
    #: Accepted relative change for ``ratio`` rules.
    tolerance: float = 0.0


@dataclass(frozen=True)
class Finding:
    """One difference between a run and its baseline."""

    kind: FindingKind
    suite: str
    scenario: str
    metric: str = ""
    baseline: Any = None
    actual: Any = None
    message: str = ""

    @property
    def failing(self) -> bool:
        return self.kind in FAILING_KINDS

    def describe(self) -> str:
        """One readable line, e.g. ``search_basic.tool_call_count: changed 3 -> 4 (...)``."""

        target = f"{self.scenario}.{self.metric}" if self.metric else self.scenario
        line = f"{target}: {self.kind.replace('_', ' ')}"
        if self.baseline is not None or self.actual is not None:
            line = f"{line} {self.baseline!r} -> {self.actual!r}"
        return f"{line} ({self.message})" if self.message else line


@dataclass(frozen=True)
class Baseline:
    """A parsed v2 baseline file."""

    schema_version: int
    suite: str
    scenarios: Mapping[str, Mapping[str, Any]]
    #: Rows of the runs with a memory seed; ``None`` when the file has no such block.
    scenarios_with_memory: Mapping[str, Mapping[str, Any]] | None = None


@dataclass(frozen=True)
class ComparisonReport:
    """The findings of one comparison."""

    suite: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        return any(finding.failing for finding in self.findings)


def load_baseline(path: Path) -> Baseline:
    """Read a v2 baseline; :class:`BaselineError` for a missing or malformed file."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise BaselineError(f"{path} is not a schema_version {BASELINE_SCHEMA_VERSION} baseline")
    scenarios = data.get(SCENARIOS_BLOCK)
    memory = data.get(MEMORY_BLOCK)
    if not _is_block(scenarios) or (memory is not None and not _is_block(memory)):
        raise BaselineError(f"{path}: every block must map scenario names to metric records")
    for block_name, block in ((SCENARIOS_BLOCK, scenarios), (MEMORY_BLOCK, memory or {})):
        for name, row in block.items():
            for metric, value in row.items():
                # Every numeric metric is a count or a size; NaN would make no change fail.
                finite = not isinstance(value, float) or math.isfinite(value)
                if _is_number(value) and not (finite and value >= 0):
                    raise BaselineError(
                        f"{path}: {block_name}.{name}.{metric} must be a finite number >= 0,"
                        f" not {value!r}"
                    )
    return Baseline(
        schema_version=BASELINE_SCHEMA_VERSION,
        suite=str(data.get("suite", "") or ""),
        scenarios=scenarios,
        scenarios_with_memory=memory,
    )


def baseline_document(
    suite: str,
    scenarios: Mapping[str, Mapping[str, Any]],
    scenarios_with_memory: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The JSON document of a v2 baseline, rows sorted by scenario name."""

    document: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "suite": suite,
        SCENARIOS_BLOCK: _sorted_block(scenarios),
    }
    if scenarios_with_memory is not None:
        document[MEMORY_BLOCK] = _sorted_block(scenarios_with_memory)
    return document


def deterministic_rules(settings: PipelineSettings) -> tuple[MetricRule, ...]:
    """Rules of the deterministic suites (scripted evals): exact, except ``request_chars``."""

    exact = (
        "terminal_status",
        "trace_complete",
        "final_answer",
        "model_turn_count",
        "tool_call_count",
        "policy_block_count",
        "repeated_action_count",
        "approval_request_count",
        "error_count",
        "observation_count",
        "action_signature",
    )
    return (
        *(MetricRule(metric, "exact") for metric in exact),
        MetricRule("request_chars", "ratio", tolerance=settings.request_chars_tolerance),
    )


def compare(
    results: Mapping[str, Mapping[str, Any]],
    rows: Mapping[str, Mapping[str, Any]],
    rules: Sequence[MetricRule],
    *,
    suite: str,
) -> ComparisonReport:
    """Compare per-scenario ``results`` with baseline ``rows`` under ``rules``.

    A scenario without a row is ``missing_baseline`` and a row without a scenario is
    ``orphan_baseline``, so a scenario cannot disappear silently.
    """

    findings: list[Finding] = []
    for name in sorted(set(results) | set(rows)):
        if name not in rows:
            findings.append(
                Finding("missing_baseline", suite, name, message="scenario has no baseline row")
            )
            continue
        if name not in results:
            findings.append(
                Finding("orphan_baseline", suite, name, message="baseline row has no scenario")
            )
            continue
        for rule in rules:
            finding = _check(rule, results[name], rows[name], suite=suite, scenario=name)
            if finding is not None:
                findings.append(finding)
    return ComparisonReport(suite=suite, findings=tuple(findings))


def _check(
    rule: MetricRule,
    actual_row: Mapping[str, Any],
    baseline_row: Mapping[str, Any],
    *,
    suite: str,
    scenario: str,
) -> Finding | None:
    actual = actual_row.get(rule.metric)
    expected = baseline_row.get(rule.metric)
    if actual == expected:
        return None
    if rule.kind == "ratio" and _is_number(actual) and _is_number(expected):
        return _ratio_finding(rule, actual, expected, suite=suite, scenario=scenario)
    return Finding(
        "changed",
        suite,
        scenario,
        metric=rule.metric,
        baseline=expected,
        actual=actual,
        message=_direction(rule.metric, expected, actual),
    )


def _ratio_finding(
    rule: MetricRule,
    actual: float,
    expected: float,
    *,
    suite: str,
    scenario: str,
) -> Finding | None:
    if expected == 0:
        change = float("inf") if actual > 0 else float("-inf")
    else:
        change = (actual - expected) / expected
    if abs(change) <= rule.tolerance:
        return None
    kind: FindingKind = "regression" if change > 0 else "improvement"
    word = "more expensive" if change > 0 else "cheaper"
    return Finding(
        kind,
        suite,
        scenario,
        metric=rule.metric,
        baseline=expected,
        actual=actual,
        message=f"{change:+.1%}, {word} than the {rule.tolerance:.0%} tolerance allows",
    )


def _direction(metric: str, expected: Any, actual: Any) -> str:
    if expected is None:
        return "metric missing from the baseline row"
    if actual is None:
        return "metric missing from the run"
    if metric in _FEWER_IS_BETTER and _is_number(actual) and _is_number(expected):
        if actual < expected:
            return "fewer than the baseline, probably better; update the baseline if intended"
        return "more than the baseline"
    return "differs from the baseline"


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_block(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(row, dict) for row in value.values())


def _sorted_block(block: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: dict(sorted(block[name].items())) for name in sorted(block)}


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "Baseline",
    "BaselineError",
    "ComparisonReport",
    "FAILING_KINDS",
    "Finding",
    "FindingKind",
    "MEMORY_BLOCK",
    "MetricKind",
    "MetricRule",
    "SCENARIOS_BLOCK",
    "baseline_document",
    "compare",
    "deterministic_rules",
    "load_baseline",
]
