"""Neutral browser contract package."""

from __future__ import annotations

from src.browser.errors import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    BrowserErrorCode,
)

__all__ = [
    "BROWSER_ERROR_ACTION_FAILED",
    "BROWSER_ERROR_INVALID_REF",
    "BrowserErrorCode",
]
