from __future__ import annotations

import importlib.util
from typing import get_args

from src.browser import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BrowserErrorCode,
)


def test_there_is_no_browser_tool_vocabulary_any_more() -> None:
    # Browser tools are recognised by their server and annotations, never by name.
    for module in ("src.browser.names", "src.browser.roles", "src.browser.normalization"):
        assert importlib.util.find_spec(module) is None, module


def test_browser_error_codes_export_shared_vocabulary() -> None:
    assert BROWSER_ERROR_INVALID_REF == "invalid_ref"
    assert BROWSER_ERROR_ACTION_FAILED == "action_failed"
    assert set(get_args(BrowserErrorCode)) == {"invalid_ref", "action_failed"}
