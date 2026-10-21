"""Import fences around the UI layer (docs/development/2026-10-19-gui-redesign-design.md §5.1).

- ``src/agent_loop/**`` and ``src/harness/**`` import neither ``src.ui`` nor ``src.cli``: the
  engine and the harness know nothing about front ends.
- ``src/ui/**`` does not import ``src.agent_loop.execution``: a front end talks to the
  session through ``SessionController`` and the event stream, never to the engine.
- ``src/ui/**`` does not import ``src.cli``: the CLI builds on the UI layer, not the reverse.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORE_MODULES = sorted([*(SRC / "agent_loop").rglob("*.py"), *(SRC / "harness").rglob("*.py")])
UI_MODULES = sorted((SRC / "ui").rglob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def _under(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


@pytest.mark.parametrize("path", CORE_MODULES, ids=lambda path: str(path.relative_to(SRC)))
def test_the_engine_and_the_harness_import_no_front_end(path: Path) -> None:
    offending = [
        module
        for module in _imported_modules(path)
        if _under(module, "src.ui") or _under(module, "src.cli")
    ]
    assert offending == []


@pytest.mark.parametrize("path", UI_MODULES, ids=lambda path: str(path.relative_to(SRC)))
def test_the_ui_layer_does_not_reach_into_the_engine_or_the_cli(path: Path) -> None:
    offending = [
        module
        for module in _imported_modules(path)
        if _under(module, "src.agent_loop.execution") or _under(module, "src.cli")
    ]
    assert offending == []


def test_the_ui_layer_exists() -> None:
    assert (SRC / "ui" / "controller.py") in UI_MODULES
