#!/usr/bin/env python3
"""Run AutoBrowser scenario evals against the engine-native loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.evals import assert_scenario_result, run_scenario
from tests.evals.runner import load_scenarios


async def _run() -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for scenario in load_scenarios():
        result = await run_scenario(scenario)
        assert_scenario_result(scenario, result)
        results[scenario.name] = {
            key: value
            for key, value in result.metrics().items()
            if key != "final_answer"
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/evals/baselines/agent_loop_v1.json"),
        help="Baseline metrics JSON to compare against.",
    )
    args = parser.parse_args()
    results = asyncio.run(_run())
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if results != baseline:
        print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
