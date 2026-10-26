"""The generated eval scenarios stay in sync with scripts/generate_eval_scenarios.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import generate_eval_scenarios as generator
from tests.evals.runner import SCENARIO_DIR


@pytest.mark.parametrize("scenario", generator.SCENARIOS, ids=lambda scenario: scenario["name"])
def test_the_committed_yaml_is_the_generator_output(scenario: dict[str, Any]) -> None:
    path = SCENARIO_DIR / f"{scenario['name']}.yaml"

    assert path.read_text(encoding="utf-8") == generator.render(scenario), (
        f"{path} differs from the generator; run `uv run scripts/generate_eval_scenarios.py`"
    )


@pytest.mark.parametrize("scenario", generator.SCENARIOS, ids=lambda scenario: scenario["name"])
def test_the_simulator_finds_no_problem(scenario: dict[str, Any]) -> None:
    _metrics, _errors, problems = generator.simulate(scenario)

    assert problems == []


def test_every_generated_scenario_is_strict() -> None:
    assert all(scenario["strict"] is True for scenario in generator.SCENARIOS)


def test_the_generator_writes_into_a_given_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predicted = tmp_path / "predicted.json"
    monkeypatch.setattr(
        "sys.argv", ["generate", "--out", str(tmp_path / "out"), "--predicted", str(predicted)]
    )

    assert generator.main() == 0
    assert len(list((tmp_path / "out").glob("*.yaml"))) == len(generator.SCENARIOS)
    assert predicted.exists()
