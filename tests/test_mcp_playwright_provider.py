from __future__ import annotations

from typing import Any

from src.browser.adapters import PlaywrightMCPBrowserProvider


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
