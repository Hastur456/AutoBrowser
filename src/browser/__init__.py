"""Neutral browser contract package."""

from __future__ import annotations

from src.browser.adapters import PlaywrightMCPBrowserProvider, element_description_from_snapshot
from src.browser.contracts import BrowserAction, BrowserActionName, BrowserResult
from src.browser.errors import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BROWSER_ERROR_UNKNOWN_ACTION,
    BrowserErrorCode,
)
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
    "PlaywrightMCPBrowserProvider",
    "element_description_from_snapshot",
]
