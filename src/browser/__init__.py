"""Neutral browser contract package."""

from __future__ import annotations

from src.browser.adapters import (
    CANONICAL_TO_PLAYWRIGHT,
    PLAYWRIGHT_TO_CANONICAL,
    PlaywrightMCPBrowserProvider,
    element_description_from_snapshot,
    is_browser_snapshot_name,
    is_browser_tool_name,
    to_canonical_browser_name,
    to_playwright_browser_name,
)
from src.browser.contracts import BrowserAction, BrowserActionName, BrowserResult
from src.browser.errors import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BROWSER_ERROR_UNKNOWN_ACTION,
    BrowserErrorCode,
)
from src.browser.fake import FakeBrowserProvider
from src.browser.provider import BrowserProvider

__all__ = [
    "BROWSER_ERROR_ACTION_FAILED",
    "BROWSER_ERROR_INVALID_REF",
    "BROWSER_ERROR_UNKNOWN_ACTION",
    "BrowserAction",
    "BrowserActionName",
    "BrowserErrorCode",
    "BrowserProvider",
    "BrowserResult",
    "CANONICAL_TO_PLAYWRIGHT",
    "FakeBrowserProvider",
    "PLAYWRIGHT_TO_CANONICAL",
    "PlaywrightMCPBrowserProvider",
    "element_description_from_snapshot",
    "is_browser_snapshot_name",
    "is_browser_tool_name",
    "to_canonical_browser_name",
    "to_playwright_browser_name",
]
