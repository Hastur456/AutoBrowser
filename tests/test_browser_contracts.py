from __future__ import annotations

from typing import get_args

from src.browser import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BROWSER_ERROR_UNKNOWN_ACTION,
    BrowserAction,
    BrowserActionName,
    BrowserErrorCode,
    FakeBrowserProvider,
    BrowserProvider,
    BrowserResult,
    CANONICAL_TO_PLAYWRIGHT,
    PLAYWRIGHT_TO_CANONICAL,
    is_browser_snapshot_name,
    is_browser_tool_name,
    to_canonical_browser_name,
    to_playwright_browser_name,
)


def test_browser_action_names_cover_canonical_contract() -> None:
    assert set(get_args(BrowserActionName)) == {
        "browser.navigate",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.hover",
        "browser.evaluate",
    }


def test_browser_tool_name_helpers_bridge_canonical_and_playwright_names() -> None:
    assert CANONICAL_TO_PLAYWRIGHT["browser.snapshot"] == "browser_snapshot"
    assert CANONICAL_TO_PLAYWRIGHT["browser.click"] == "browser_click"
    assert CANONICAL_TO_PLAYWRIGHT["browser.type"] == "browser_type"
    assert PLAYWRIGHT_TO_CANONICAL["browser_snapshot"] == "browser.snapshot"
    assert to_playwright_browser_name("browser.click") == "browser_click"
    assert to_canonical_browser_name("browser_type") == "browser.type"
    assert is_browser_tool_name("browser.missing")
    assert is_browser_snapshot_name("browser.snapshot")
    assert is_browser_snapshot_name("browser_snapshot")


def test_browser_action_shape_matches_existing_tool_request_fields() -> None:
    action: BrowserAction = {
        "name": "browser.click",
        "args": {"ref": "e14"},
        "reason": "Click the visible catalog button.",
        "id": "call_1",
    }

    assert action["name"] == "browser.click"
    assert action["args"] == {"ref": "e14"}
    assert BrowserAction.__required_keys__ == frozenset()
    assert BrowserAction.__optional_keys__ == frozenset({"name", "args", "reason", "id"})


def test_browser_result_shape_supports_optional_error_code() -> None:
    result: BrowserResult = {
        "name": "browser.click",
        "status": "error",
        "content": "",
        "error": "Ref e14 not found",
        "error_code": BROWSER_ERROR_INVALID_REF,
    }

    assert result["error_code"] == "invalid_ref"
    assert BrowserResult.__required_keys__ == frozenset()
    assert BrowserResult.__optional_keys__ == frozenset(
        {"name", "status", "content", "error", "error_code"}
    )


def test_browser_error_codes_export_shared_vocabulary() -> None:
    assert BROWSER_ERROR_INVALID_REF == "invalid_ref"
    assert BROWSER_ERROR_UNKNOWN_ACTION == "unknown_action"
    assert BROWSER_ERROR_ACTION_FAILED == "action_failed"
    assert set(get_args(BrowserErrorCode)) == {
        "invalid_ref",
        "unknown_action",
        "action_failed",
    }


def test_browser_provider_protocol_matches_expected_shape() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])

    assert isinstance(provider, BrowserProvider)
