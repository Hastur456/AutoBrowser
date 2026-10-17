"""The engine stays server-neutral: no browser imports and no browser tool names.

Browser knowledge (which tool takes the snapshot, which tools act on the page) reaches the
engine through ``EngineResources.tool_roles``; see
``docs/development/2026-10-17-explicit-agent-objects-plan.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "agent_loop" / "execution"
ENGINE_MODULES = sorted(ENGINE.glob("*.py"))

# A word starting with ``browser_`` is a browser tool name (``browser_snapshot``,
# ``browser_tabs``) or the prefix itself; state fields such as ``last_browser_action`` do not
# match.
BROWSER_TOOL_NAME = re.compile(r"\bbrowser_\w*")


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


@pytest.mark.parametrize("path", ENGINE_MODULES, ids=lambda path: path.name)
def test_the_engine_imports_nothing_from_the_browser_layer(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    browser_imports = [
        module
        for module in _imported_modules(tree)
        if module == "src.browser" or module.startswith("src.browser.")
    ]

    assert not browser_imports, f"{path.name} imports {browser_imports}"


@pytest.mark.parametrize("path", ENGINE_MODULES, ids=lambda path: path.name)
def test_the_engine_names_no_browser_tools(path: Path) -> None:
    found = BROWSER_TOOL_NAME.findall(path.read_text(encoding="utf-8"))

    assert not found, f"{path.name} mentions {sorted(set(found))}"


def test_the_fence_sees_the_engine_modules() -> None:
    assert {"loop.py", "observation.py", "guards.py", "resources.py"} <= {
        path.name for path in ENGINE_MODULES
    }
