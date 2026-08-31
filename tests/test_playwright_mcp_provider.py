from __future__ import annotations

from typing import Any

import pytest

from src.agent_loop.execution.tools import ToolBroker
from src.browser import BROWSER_ERROR_INVALID_REF, BROWSER_ERROR_UNKNOWN_ACTION
from src.browser.adapters import CANONICAL_TO_PLAYWRIGHT, PlaywrightMCPBrowserProvider
from src.harness.tools import ToolRegistry


class FakeTool:
    def __init__(self, name: str, schema: dict[str, Any]) -> None:
        self.name = name
        self.args_schema = schema


def test_provider_passes_through_non_browser_requests() -> None:
    tool = FakeTool("custom_tool", {"properties": {"url": {"type": "string"}}})
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "custom_tool", "args": {"url": "https://example.com"}},
        {},
    )

    assert request["args"] == {"url": "https://example.com"}


def test_provider_maps_ref_to_target() -> None:
    tool = FakeTool("browser_click", {"properties": {"target": {"type": "string"}}})
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser_click", "args": {"ref": "e14"}},
        {},
    )

    assert request["args"] == {"ref": "e14", "target": "e14"}


def test_provider_maps_canonical_name_to_playwright_tool_name() -> None:
    tool = FakeTool("browser_click", {"properties": {"target": {"type": "string"}}})
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser.click", "args": {"ref": "e14"}},
        {},
    )

    assert request["name"] == CANONICAL_TO_PLAYWRIGHT["browser.click"]
    assert request["args"] == {"ref": "e14", "target": "e14"}


def test_provider_maps_canonical_snapshot_name_to_playwright_tool_name() -> None:
    tool = FakeTool("browser_snapshot", {"properties": {"depth": {"type": "integer"}}})
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser.snapshot", "args": {"depth": 5}},
        {},
    )

    assert request["name"] == CANONICAL_TO_PLAYWRIGHT["browser.snapshot"]
    assert request["args"] == {"depth": 5}


def test_provider_maps_target_to_ref_for_legacy_schema() -> None:
    tool = FakeTool("browser_hover", {"properties": {"ref": {"type": "string"}}})
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser_hover", "args": {"target": "e14"}},
        {},
    )

    assert request["args"] == {"target": "e14", "ref": "e14"}


def test_provider_adds_element_from_snapshot() -> None:
    tool = FakeTool(
        "browser_click",
        {
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
            }
        }
    )
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser_click", "args": {"ref": "e14"}},
        {"snapshot": '- button "Catalog" ref=e14'},
    )

    assert request["args"] == {"ref": "e14", "element": 'button "Catalog"'}


def test_provider_falls_back_to_ref_when_snapshot_line_is_missing() -> None:
    tool = FakeTool(
        "browser_click",
        {
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
            }
        }
    )
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser_click", "args": {"ref": "e14"}},
        {"snapshot": '- button "Search" ref=e8'},
    )

    assert request["args"] == {"ref": "e14", "element": "e14"}


def test_provider_filters_args_when_schema_forbids_additional_properties() -> None:
    tool = FakeTool(
        "browser_click",
        {
            "properties": {"target": {"type": "string"}},
            "additionalProperties": False,
        }
    )
    provider = PlaywrightMCPBrowserProvider([tool])

    request = provider.normalize_request(
        {"name": "browser_click", "args": {"ref": "e14", "extra": "ignored"}},
        {},
    )

    assert request["args"] == {"target": "e14"}


def test_provider_normalizes_invalid_ref_error_code() -> None:
    provider = PlaywrightMCPBrowserProvider()

    result = provider.normalize_result(
        {
            "name": "browser_click",
            "status": "error",
            "content": "",
            "error": "Ref e14 not found",
        }
    )

    assert result["error"] == "Ref e14 not found"
    assert result["error_code"] == BROWSER_ERROR_INVALID_REF


def test_provider_leaves_non_invalid_error_without_error_code() -> None:
    provider = PlaywrightMCPBrowserProvider()

    result = provider.normalize_result(
        {
            "name": "browser_click",
            "status": "error",
            "content": "",
            "error": "Element is not editable.",
        }
    )

    assert result["error"] == "Element is not editable."
    assert "error_code" not in result


@pytest.mark.asyncio
async def test_provider_backed_unknown_canonical_action_returns_browser_error() -> None:
    tool = FakeTool("browser_click", {"properties": {"target": {"type": "string"}}})
    provider = PlaywrightMCPBrowserProvider([tool])
    broker = ToolBroker(ToolRegistry(providers=[provider]))

    result = await broker.execute({"name": "browser.missing", "args": {}})

    assert result["status"] == "error"
    assert result["error_code"] == BROWSER_ERROR_UNKNOWN_ACTION
    assert "Unknown browser action: browser.missing" in result["error"]
