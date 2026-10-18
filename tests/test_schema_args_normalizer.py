"""SchemaArgsNormalizer: the one schema-based argument filter for every tool server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.harness.normalization import SchemaArgsNormalizer, ToolCallNormalizer


def tool(name: str, properties: dict[str, Any], additional: bool | None = None) -> Any:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if additional is not None:
        schema["additionalProperties"] = additional
    return SimpleNamespace(name=name, input_schema=schema)


def test_is_a_tool_call_normalizer() -> None:
    assert isinstance(SchemaArgsNormalizer(), ToolCallNormalizer)


def test_args_pass_through_unchanged() -> None:
    tools = {"browser_click": tool("browser_click", {"selector": {}})}
    request = {"name": "browser_click", "args": {"selector": "#go", "extra": 1}}
    out = SchemaArgsNormalizer().normalize_request(request, {"snapshot": "..."}, tools)
    assert out == {"name": "browser_click", "args": {"selector": "#go", "extra": 1}}


def test_forbidden_args_are_dropped() -> None:
    tools = {"browser_click": tool("browser_click", {"selector": {}}, additional=False)}
    request = {"name": "browser_click", "args": {"selector": "#go", "junk": 1}}
    out = SchemaArgsNormalizer().normalize_request(request, {}, tools)
    assert out["args"] == {"selector": "#go"}


def test_names_are_never_translated() -> None:
    # No canonical vocabulary: an unexposed name reaches the broker as "unknown tool".
    tools = {"browser_click": tool("browser_click", {"ref": {}})}
    out = SchemaArgsNormalizer().normalize_request(
        {"name": "browser.click", "args": {"ref": "e1"}}, {}, tools
    )
    assert out == {"name": "browser.click", "args": {"ref": "e1"}}


def test_any_server_is_treated_alike() -> None:
    # No tool family is special-cased: an unknown tool passes through, a known one is filtered.
    assert SchemaArgsNormalizer().normalize_request(
        {"name": "fs__read", "args": {"path": "a"}}, {}, {}
    ) == {"name": "fs__read", "args": {"path": "a"}}
    tools = {"fs__read": tool("fs__read", {"path": {}}, additional=False)}
    out = SchemaArgsNormalizer().normalize_request(
        {"name": "fs__read", "args": {"path": "a", "junk": 1}}, {}, tools
    )
    assert out["args"] == {"path": "a"}


def test_results_pass_through() -> None:
    result = {"name": "browser_click", "status": "error", "content": "", "error": "boom"}
    assert SchemaArgsNormalizer().normalize_result(result) == result
