"""Cross-cutting memory invariants (plan §12): layering, evals, and no leakage into prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent_loop.evals import run_scenario
from tests.evals.runner import load_scenarios, seed_memory

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "agent_loop" / "execution"


@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")), ids=lambda path: path.name)
def test_the_engine_knows_no_memory_files_or_memory_tool_names(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    for forbidden in (
        "memory_store",
        "memory_tool",
        "memory_consolidation",
        "src.browser.memory",
        "memory_view",
        "memory_write",
    ):
        assert forbidden not in source, f"{path.name} mentions {forbidden}"


def test_memory_files_are_reached_only_through_the_session() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        if path.name in {"session.py", "memory_store.py", "memory_tool.py", "memory_consolidation.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "from src.harness.memory_store" not in source, path
        assert "from src.harness.memory_tool" not in source, path


@pytest.mark.asyncio
async def test_seed_memory_does_not_change_any_eval_outcome() -> None:
    for scenario in load_scenarios():
        plain = await run_scenario(scenario)
        with_memory = await run_scenario(scenario, memory=seed_memory())

        assert with_memory.metrics() == plain.metrics(), scenario.name
        assert with_memory.prompt_chars > plain.prompt_chars, scenario.name


def test_the_seed_memory_renders_the_wildcard_procedure_everywhere() -> None:
    text = seed_memory().render({"snapshot": "- Page URL: https://example.com/"})

    assert "- procedures/search.md [user]" in text
    assert "- sites/ozon.ru.md [user]" in text
    assert "For example.com:\n### procedures/search.md [user]" in text
    assert "ozon.ru/search/?text=" not in text  # the Ozon body only on Ozon
