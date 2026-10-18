"""No logic keyed on tool names: the harness works with whatever tools the servers expose.

The engine imports nothing from ``src/browser/``. Nothing under ``src/`` decides by a
``browser_*`` tool name: kept snapshots come from the MCP ``readOnlyHint`` per server, prompt
resources from the session's configuration, argument filtering from the tool's schema. See
``docs/decisions/2026-10-19-name-free-tool-handling.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENGINE = SRC / "agent_loop" / "execution"
ENGINE_MODULES = sorted(ENGINE.glob("*.py"))

#: The prompts are revised by the user separately and still describe Playwright tools.
PROMPT_MODULES = {SRC / "agent_loop" / "prompts.py"}
SOURCE_MODULES = sorted(path for path in SRC.rglob("*.py") if path not in PROMPT_MODULES)

# A word starting with ``browser_`` is a browser tool name (``browser_snapshot``,
# ``browser_tabs``) or the prefix itself; state fields such as ``last_browser_action`` do not
# match.
BROWSER_TOOL_NAME = re.compile(r"\bbrowser_\w*")
#: A string literal that is a browser tool name or the bare prefix.
BROWSER_NAME_LITERAL = re.compile(r"^browser_\w*$")
#: Literals of that shape that are not tool names.
NOT_TOOL_NAMES = {"browser_mcp_server"}  # the setting that names the browser server


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def _all_entries(tree: ast.Module) -> set[int]:
    """Ids of the string nodes listed in ``__all__`` (export names, not data)."""

    ids: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            ids.update(id(item) for item in ast.walk(node.value))
    return ids


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


@pytest.mark.parametrize(
    "path", SOURCE_MODULES, ids=lambda path: path.relative_to(SRC).as_posix()
)
def test_no_source_module_decides_by_a_browser_tool_name(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exported = _all_entries(tree)

    literals = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and BROWSER_NAME_LITERAL.match(node.value)
            and node.value not in NOT_TOOL_NAMES
            and id(node) not in exported
        }
    )

    assert not literals, f"{path.relative_to(SRC)} uses tool-name literals {literals}"


def test_the_fence_sees_the_source_modules() -> None:
    names = {path.relative_to(SRC).as_posix() for path in SOURCE_MODULES}

    assert {"agent_loop/execution/loop.py", "harness/session.py", "browser/permissions.py"} <= names
    assert "agent_loop/prompts.py" not in names
