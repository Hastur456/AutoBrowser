"""Cross-cutting memory invariants (plan §12): layering, evals, and no leakage into prompts."""

from __future__ import annotations

import ast
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


MEMORY_MODULES = (
    ROOT / "src" / "harness" / "memory.py",
    ROOT / "src" / "harness" / "memory_store.py",
    ROOT / "src" / "harness" / "memory_tool.py",
    ROOT / "src" / "harness" / "memory_consolidation.py",
    ROOT / "src" / "browser" / "memory.py",
    ROOT / "src" / "agent_loop" / "execution" / "notes.py",
)


@pytest.mark.parametrize("path", MEMORY_MODULES, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_memory_limits_live_in_the_settings_not_in_module_constants(path: Path) -> None:
    """Every memory tunable is a ``MemorySettings`` field (``src/config.py``), never a module
    constant such as ``MAX_ENTRIES = 3``."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        value = getattr(node, "value", None)
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or value is None:
            continue
        numeric = isinstance(value, ast.Constant) and type(value.value) in (int, float)
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        assert not numeric, f"{path.name}: {names} is a numeric module constant; move it to MemorySettings"


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
