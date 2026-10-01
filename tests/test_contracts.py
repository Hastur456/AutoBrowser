"""Tests for the neutral contracts leaf (:mod:`src.contracts`)."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from src.contracts import HookEvent, HookResult

CONTRACTS_PATH = Path(__file__).resolve().parents[1] / "src" / "contracts.py"


def test_hook_event_is_json_serializable() -> None:
    event = HookEvent(
        name="stop",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task="find 3 jackets",
        tool="browser_navigate",
        server="playwright",
        args={"url": "https://example.com", "nested": {"n": [1, 2]}},
        result={"name": "browser_navigate", "status": "success", "content": "ok"},
        reason="needs approval",
        final_answer="Jacket A costs 1 299 ₽",
        evidence=("snapshot text", "observation text"),
        stop_hook_active=True,
        status="done",
    )

    decoded = json.loads(json.dumps(asdict(event), ensure_ascii=False))

    assert decoded["name"] == "stop"
    assert decoded["evidence"] == ["snapshot text", "observation text"]
    assert decoded["args"]["nested"] == {"n": [1, 2]}


def test_hook_event_defaults_are_empty_and_independent() -> None:
    first = HookEvent(name="goal_start", session_id=None, goal_id="g", task_id="t", task="x")
    second = HookEvent(name="goal_start", session_id=None, goal_id="g", task_id="t", task="x")

    assert first.args == {} and first.result == {} and first.evidence == ()
    assert first.args is not second.args


def test_hook_result_defaults_mean_no_opinion() -> None:
    result = HookResult()

    assert result.decision is None
    assert result.updated_input is None
    assert result.updated_output is None
    assert result.additional_context == ""


def test_hook_contracts_are_frozen() -> None:
    with pytest.raises(AttributeError):
        HookResult().decision = "deny"  # type: ignore[misc]


def test_contracts_import_only_the_standard_library() -> None:
    """``src/contracts.py`` is a neutral leaf every layer may depend on."""

    tree = ast.parse(CONTRACTS_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed in src/contracts.py"
            imported.add(str(node.module).split(".")[0])

    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert imported <= allowed, f"non-stdlib imports: {sorted(imported - allowed)}"
