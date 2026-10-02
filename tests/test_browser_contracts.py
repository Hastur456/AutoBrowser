from __future__ import annotations

from typing import get_args

from src.browser import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BrowserErrorCode,
)
from src.browser.names import (
    SNAPSHOT_TOOL,
    TABS_TOOL,
    is_browser_snapshot_name,
    is_browser_tool_name,
)


def test_browser_tool_names_are_the_exposed_names() -> None:
    assert SNAPSHOT_TOOL == "browser_snapshot"
    assert TABS_TOOL == "browser_tabs"
    assert is_browser_tool_name("browser_missing")
    assert not is_browser_tool_name("fs__read")
    assert is_browser_snapshot_name("browser_snapshot")


def test_the_old_dotted_vocabulary_is_gone() -> None:
    assert not is_browser_tool_name("browser.click")
    assert not is_browser_snapshot_name("browser.snapshot")


def test_browser_error_codes_export_shared_vocabulary() -> None:
    assert BROWSER_ERROR_INVALID_REF == "invalid_ref"
    assert BROWSER_ERROR_ACTION_FAILED == "action_failed"
    assert set(get_args(BrowserErrorCode)) == {"invalid_ref", "action_failed"}
