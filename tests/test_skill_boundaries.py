"""Cross-cutting skill invariants: layering, settings-only limits, valid built-in skills."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.config import SkillsSettings
from src.harness.skills import SkillStore, builtin_skills_root

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "agent_loop" / "execution"
SKILL_MODULES = (
    ROOT / "src" / "harness" / "skills.py",
    ROOT / "src" / "harness" / "skill_tool.py",
)


@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")), ids=lambda path: path.name)
def test_the_engine_knows_no_skill_modules_or_the_skill_tool_name(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    for forbidden in ("src.harness.skills", "src.harness.skill_tool", "skill_view", "SKILL.md"):
        assert forbidden not in source, f"{path.name} mentions {forbidden}"


def test_the_context_assembler_knows_no_skills() -> None:
    source = (ROOT / "src" / "agent_loop" / "context.py").read_text(encoding="utf-8")

    assert "src.harness.skill" not in source
    assert "skill_view" not in source


def test_skill_modules_are_reached_only_through_the_session_evals_and_doctor() -> None:
    allowed = {"session.py", "skills.py", "skill_tool.py", "evals.py", "subcommands.py"}
    for path in (ROOT / "src").rglob("*.py"):
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "from src.harness.skills" not in source, path
        assert "from src.harness.skill_tool" not in source, path


@pytest.mark.parametrize("path", SKILL_MODULES, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_skill_limits_live_in_the_settings_not_in_module_constants(path: Path) -> None:
    """Every skill tunable is a ``SkillsSettings`` field (``src/config.py``)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        value = node.value
        numeric = isinstance(value, ast.Constant) and type(value.value) in (int, float)
        assert not numeric, f"{path.name}: numeric module constant; move it to SkillsSettings"


def test_every_built_in_skill_is_valid() -> None:
    store = SkillStore.load(
        SkillsSettings(),
        roots=[("builtin", builtin_skills_root())],
        capabilities={"browser"},
    )

    assert store.problems() == ()
    assert [skill.name for skill in store.skills()] == ["browser-agent-rules"]
    assert [skill.name for skill in store.always()] == ["browser-agent-rules"]
