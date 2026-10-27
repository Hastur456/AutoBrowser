#!/usr/bin/env python3
"""Run AutoBrowser scenario evals and compare them with a v2 baseline.

Every scenario runs to the end. Failed scenario assertions, an exhausted strict script and every
metric that differs from the baseline are printed as findings, one line each; the exit code is
0 (no failing finding), 1 (a regression or change) or 2 (a missing or malformed baseline).

``--memory-seed DIR`` runs every scenario a second time with a read-only ``Memory`` block over
DIR (``tests/evals/memory_seed`` is the shipped seed) and compares it with the baseline's
``scenarios_with_memory`` block: memory must not change the outcome, only the request size.

``--write-baseline`` is the explicit way to update the baseline: it prints the findings against
the current file, then writes the run's metrics there (keeping the memory block when no seed is
given). It refuses while a scenario assertion fails. CI never writes a baseline.

The run is hermetic: the personal ``config.yaml``, ``.env`` and ``AUTOBROWSER_*`` variables
are hidden (``tests/evals/runner.isolate_settings``), so the metrics are those of the code
defaults on every machine. Only ``AUTOBROWSER_PIPELINE__*`` (the comparison tolerances) is
kept.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.comparison import (
    Baseline,
    BaselineError,
    Finding,
    baseline_document,
    compare,
    deterministic_rules,
    load_baseline,
)
from src.agent_loop.evals import ScenarioRun, collect_eval_metrics, run_findings
from src.config import PipelineSettings, get_settings
from tests.evals.runner import isolate_settings, load_scenarios, seed_memory

DEFAULT_BASELINE = ROOT / "tests" / "evals" / "baselines" / "agent_loop_v2.json"
SUITE = "agent_loop"
MEMORY_SUITE = "agent_loop+memory"


def _records(runs: list[ScenarioRun]) -> dict[str, dict[str, object]]:
    return {run.name: run.result.baseline_metrics() for run in runs}


def _findings(
    runs: list[ScenarioRun],
    rows: dict[str, dict[str, object]] | None,
    *,
    suite: str,
    settings: PipelineSettings,
) -> list[Finding]:
    findings = run_findings(runs, suite=suite)
    if rows is not None:
        report = compare(_records(runs), rows, deterministic_rules(settings), suite=suite)
        findings.extend(report.findings)
    return findings


def _print_findings(suite: str, runs: list[ScenarioRun], findings: list[Finding]) -> None:
    failing = [finding for finding in findings if finding.failing]
    if not failing:
        print(f"{suite}: {len(runs)} scenarios match the baseline")
    for finding in findings:
        print(f"{suite}: {finding.describe()}")
    for run in runs:
        if run.problems:
            print(f"{suite}: action sequence of {run.name}")
            for line in run.result.action_sequence.splitlines():
                print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="v2 baseline JSON to compare against (and to write with --write-baseline).",
    )
    parser.add_argument(
        "--memory-seed",
        type=Path,
        default=None,
        help="Also run with a read-only Memory block over this seed directory.",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Write the run's metrics to --baseline after printing the findings.",
    )
    args = parser.parse_args()
    settings = get_settings().pipeline

    baseline: Baseline | None = None
    try:
        baseline = load_baseline(args.baseline)
    except BaselineError as exc:
        if not (args.write_baseline and not args.baseline.exists()):
            print(f"error: {exc}", file=sys.stderr)
            return 2

    scenarios = load_scenarios()
    runs = asyncio.run(collect_eval_metrics(scenarios))
    findings = _findings(
        runs, baseline.scenarios if baseline else None, suite=SUITE, settings=settings
    )
    _print_findings(SUITE, runs, findings)

    memory_runs: list[ScenarioRun] | None = None
    if args.memory_seed is not None:
        seed = args.memory_seed
        memory_runs = asyncio.run(
            collect_eval_metrics(scenarios, memory_factory=lambda: seed_memory(seed))
        )
        memory_rows = baseline.scenarios_with_memory if baseline else None
        if baseline is not None and memory_rows is None and not args.write_baseline:
            print(f"error: {args.baseline} has no scenarios_with_memory block", file=sys.stderr)
            return 2
        memory_findings = _findings(memory_runs, memory_rows, suite=MEMORY_SUITE, settings=settings)
        _print_findings(MEMORY_SUITE, memory_runs, memory_findings)
        findings.extend(memory_findings)
        memory_chars = {run.name: run.result.request_chars for run in memory_runs}
        report = {
            run.name: {
                "without_memory": run.result.request_chars,
                "with_memory": memory_chars[run.name],
            }
            for run in sorted(runs, key=lambda run: run.name)
        }
        print(json.dumps({"request_chars": report}, ensure_ascii=False, indent=2))

    if args.write_baseline:
        if any(run.problems for run in [*runs, *(memory_runs or [])]):
            print("error: not writing a baseline while a scenario assertion fails", file=sys.stderr)
            return 1
        if memory_runs is not None:
            memory_block = _records(memory_runs)
        else:
            memory_block = baseline.scenarios_with_memory if baseline else None
        document = baseline_document(SUITE, _records(runs), memory_block)
        args.baseline.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.baseline}")
        return 0
    return 1 if any(finding.failing for finding in findings) else 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="autobrowser-evals-") as home:
        isolate_settings(Path(home))
        raise SystemExit(main())
