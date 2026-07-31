from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent_loop.evals import assert_scenario_result, run_scenario
from tests.evals.runner import load_scenarios

BASELINE_PATH = Path("tests/evals/baselines/langgraph_v1.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda scenario: scenario.name)
async def test_eval_scenarios_pass_configured_assertions(scenario) -> None:
    result = await run_scenario(scenario)

    assert_scenario_result(scenario, result)


@pytest.mark.asyncio
async def test_eval_scenarios_match_langgraph_v1_baseline() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    results = {}
    for scenario in load_scenarios():
        result = await run_scenario(scenario)
        results[scenario.name] = {
            key: value
            for key, value in result.metrics().items()
            if key != "final_answer"
        }

    assert results == baseline
