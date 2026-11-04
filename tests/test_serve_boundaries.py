"""``src/serve`` is a client of ``SessionController``: it never reaches into the engine, the
browser layer, the CLI composition, the providers or the harness, and nothing below it
imports it back (``docs/development/2026-11-02-serve-mode-design.md`` N2)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "src" / "serve"

#: What the stand may not import.
FORBIDDEN_IN_SERVE = (
    "src.agent_loop.execution",
    "src.browser",
    "src.cli",
    "src.providers",
    "src.harness",
)
#: Layers that may never import the stand.
LOWER_LAYERS = ("agent_loop", "harness", "ui", "browser", "mcp", "providers")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_the_serve_package_exists() -> None:
    assert (SERVE / "__init__.py").is_file()


@pytest.mark.parametrize("path", _python_files(SERVE), ids=lambda path: path.name)
def test_the_stand_imports_only_the_controller_side(path: Path) -> None:
    for name in _imports(path):
        for forbidden in FORBIDDEN_IN_SERVE:
            assert not (name == forbidden or name.startswith(f"{forbidden}.")), (
                f"{path.name} imports {name}"
            )


@pytest.mark.parametrize(
    "path",
    [path for layer in LOWER_LAYERS for path in _python_files(ROOT / "src" / layer)],
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_no_lower_layer_imports_the_stand(path: Path) -> None:
    for name in _imports(path):
        assert not (name == "src.serve" or name.startswith("src.serve.")), f"{path} imports {name}"
