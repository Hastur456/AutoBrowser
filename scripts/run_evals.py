#!/usr/bin/env python3
"""Run AutoBrowser scenario evals against the engine-native loop.

``--memory-seed DIR`` runs every scenario a second time with a read-only ``Memory`` block over
DIR (``tests/evals/memory_seed`` is the shipped seed) and reports the prompt size of both runs;
the baseline comparison covers the memory run too, so memory must not change the outcome.
"""

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
from tests.evals.runner import load_scenarios, seed_memory


async def _run(
    memory_seed: Path | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    results: dict[str, dict[str, object]] = {}
    prompt_chars: dict[str, int] = {}
    for scenario in load_scenarios():
        memory = seed_memory(memory_seed) if memory_seed is not None else None
        result = await run_scenario(scenario, memory=memory)
        assert_scenario_result(scenario, result)
        results[scenario.name] = {
            key: value
            for key, value in result.metrics().items()
            if key != "final_answer"
        }
        prompt_chars[scenario.name] = result.prompt_chars
    return results, prompt_chars


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
    results, prompt_chars = asyncio.run(_run())
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    status = 0 if results == baseline else 1
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    if args.memory_seed is not None:
        memory_results, memory_chars = asyncio.run(_run(args.memory_seed))
        if memory_results != baseline:
            print("With memory:")
            print(json.dumps(memory_results, ensure_ascii=False, indent=2, sort_keys=True))
            status = 1
        report = {
            name: {"without_memory": prompt_chars[name], "with_memory": memory_chars[name]}
            for name in sorted(prompt_chars)
        }
        print(json.dumps({"prompt_chars": report}, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
