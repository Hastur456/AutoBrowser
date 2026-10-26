#!/usr/bin/env python3
"""Run AutoBrowser scenario evals against the engine-native loop.

Every scenario runs to the end: a failed scenario assertion and a metric that differs from the
baseline are reported per scenario (exit code 1), never raised as a traceback.

``--memory-seed DIR`` runs every scenario a second time with a read-only ``Memory`` block over
DIR (``tests/evals/memory_seed`` is the shipped seed) and reports the prompt size of both runs;
the baseline comparison covers the memory run too, so memory must not change the outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.evals import ScenarioRun, collect_eval_metrics
from tests.evals.runner import load_scenarios, seed_memory


def _v1_metrics(run: ScenarioRun) -> dict[str, object]:
    return {key: value for key, value in run.result.metrics().items() if key != "final_answer"}


def baseline_diff(
    results: Mapping[str, Mapping[str, object]],
    baseline: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """One line per scenario or metric where ``results`` and ``baseline`` disagree."""

    lines: list[str] = []
    for name in sorted(set(results) | set(baseline)):
        if name not in baseline:
            lines.append(f"{name}: no baseline row")
            continue
        if name not in results:
            lines.append(f"{name}: baseline row without a scenario")
            continue
        actual, expected = results[name], baseline[name]
        for metric in sorted(set(actual) | set(expected)):
            if actual.get(metric) != expected.get(metric):
                lines.append(
                    f"{name}.{metric}: baseline {expected.get(metric)!r}, "
                    f"actual {actual.get(metric)!r}"
                )
    return lines


def _report(label: str, runs: list[ScenarioRun], baseline: Mapping[str, Mapping[str, object]]) -> bool:
    """Print the problems of one pass; ``True`` when it has none."""

    problems = [f"{run.name}: {problem}" for run in runs for problem in run.problems]
    diff = baseline_diff({run.name: _v1_metrics(run) for run in runs}, baseline)
    if problems:
        print(f"{label}: failed scenario assertions")
        for line in problems:
            print(f"  {line}")
    if diff:
        print(f"{label}: differs from the baseline")
        for line in diff:
            print(f"  {line}")
    for run in runs:
        if run.problems:
            print(f"{label}: action sequence of {run.name}")
            for line in run.result.action_sequence.splitlines():
                print(f"  {line}")
    if not problems and not diff:
        print(f"{label}: {len(runs)} scenarios match the baseline")
    return not problems and not diff


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/evals/baselines/agent_loop_v1.json"),
        help="Baseline metrics JSON to compare against.",
    )
    parser.add_argument(
        "--memory-seed",
        type=Path,
        default=None,
        help="Also run with a read-only Memory block over this seed directory.",
    )
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    scenarios = load_scenarios()
    runs = asyncio.run(collect_eval_metrics(scenarios))
    ok = _report("evals", runs, baseline)
    if args.memory_seed is not None:
        seed = args.memory_seed
        memory_runs = asyncio.run(
            collect_eval_metrics(scenarios, memory_factory=lambda: seed_memory(seed))
        )
        ok = _report("evals with memory", memory_runs, baseline) and ok
        memory_chars = {run.name: run.result.prompt_chars for run in memory_runs}
        report = {
            run.name: {
                "without_memory": run.result.prompt_chars,
                "with_memory": memory_chars[run.name],
            }
            for run in sorted(runs, key=lambda run: run.name)
        }
        print(json.dumps({"prompt_chars": report}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
