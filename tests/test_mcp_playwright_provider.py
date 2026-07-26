from __future__ import annotations

from typing import Any

from src.mcp.playwright_provider import PlaywrightMCPBrowserProvider


class FakeTool:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.args_schema = schema


def test_provider_passes_through_non_browser_requests() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool({"properties": {"url": {"type": "string"}}})

    args = provider.prepare_args(
        tool,
        {"name": "custom_tool", "args": {"url": "https://example.com"}},
        {},
    )

    assert args == {"url": "https://example.com"}


def test_provider_maps_ref_to_target() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool({"properties": {"target": {"type": "string"}}})

    args = provider.prepare_args(
        tool,
        {"name": "browser_click", "args": {"ref": "e14"}},
        {},
    )

    assert args == {"ref": "e14", "target": "e14"}


def test_provider_maps_target_to_ref_for_legacy_schema() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool({"properties": {"ref": {"type": "string"}}})

    args = provider.prepare_args(
        tool,
        {"name": "browser_hover", "args": {"target": "e14"}},
        {},
    )

    assert args == {"target": "e14", "ref": "e14"}


def test_provider_adds_element_from_snapshot() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool(
        {
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
            }
        }
    )

    args = provider.prepare_args(
        tool,
        {"name": "browser_click", "args": {"ref": "e14"}},
        {"snapshot": '- button "Catalog" ref=e14'},
    )

    assert args == {"ref": "e14", "element": 'button "Catalog"'}


def test_provider_falls_back_to_ref_when_snapshot_line_is_missing() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool(
        {
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
            }
        }
    )

    args = provider.prepare_args(
        tool,
        {"name": "browser_click", "args": {"ref": "e14"}},
        {"snapshot": '- button "Search" ref=e8'},
    )

    assert args == {"ref": "e14", "element": "e14"}


def test_provider_filters_args_when_schema_forbids_additional_properties() -> None:
    provider = PlaywrightMCPBrowserProvider()
    tool = FakeTool(
        {
            "properties": {"target": {"type": "string"}},
            "additionalProperties": False,
        }
    )

    args = provider.prepare_args(
        tool,
        {"name": "browser_click", "args": {"ref": "e14", "extra": "ignored"}},
        {},
    )

    assert args == {"target": "e14"}
