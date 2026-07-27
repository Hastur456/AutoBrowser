from __future__ import annotations

from pathlib import Path

from src.agent_loop.evals import EvalScenario, load_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def load_scenarios() -> list[EvalScenario]:
    return [load_scenario(path) for path in sorted(SCENARIO_DIR.glob("*.yaml"))]
