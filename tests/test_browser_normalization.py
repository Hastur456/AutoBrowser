"""BrowserToolNormalizer: replaces tests/test_playwright_mcp_provider.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.browser.normalization import BrowserToolNormalizer, ToolCallNormalizer


def tool(name: str, properties: dict[str, Any], additional: bool | None = None) -> Any:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if additional is not None:
        schema["additionalProperties"] = additional
    return SimpleNamespace(name=name, input_schema=schema)


def test_is_a_tool_call_normalizer() -> None:
    assert isinstance(BrowserToolNormalizer(), ToolCallNormalizer)


def test_args_pass_through_unchanged() -> None:
    tools = {"browser_click": tool("browser_click", {"selector": {}})}
    request = {"name": "browser_click", "args": {"selector": "#go", "extra": 1}}
    out = BrowserToolNormalizer().normalize_request(request, {"snapshot": "..."}, tools)
    assert out == {"name": "browser_click", "args": {"selector": "#go", "extra": 1}}


def test_forbidden_args_are_dropped() -> None:
    tools = {"browser_click": tool("browser_click", {"selector": {}}, additional=False)}
    request = {"name": "browser_click", "args": {"selector": "#go", "junk": 1}}
    out = BrowserToolNormalizer().normalize_request(request, {}, tools)
    assert out["args"] == {"selector": "#go"}


def test_canonical_name_resolves_to_exposed_tool() -> None:
    # names.py maps the canonical dotted action to the Playwright tool name.
    tools = {"browser_click": tool("browser_click", {"ref": {}})}
    out = BrowserToolNormalizer().normalize_request(
        {"name": "browser.click", "args": {"ref": "e1"}}, {}, tools
    )
    assert out["name"] == "browser_click"


def test_non_browser_requests_pass_through() -> None:
    out = BrowserToolNormalizer().normalize_request({"name": "fs__read", "args": {"path": "a"}}, {}, {})
    assert out == {"name": "fs__read", "args": {"path": "a"}}


def test_results_pass_through() -> None:
    result = {"name": "browser_click", "status": "error", "content": "", "error": "boom"}
    assert BrowserToolNormalizer().normalize_result(result) == result
