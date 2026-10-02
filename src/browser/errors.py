"""Shared browser-layer error codes."""

from __future__ import annotations

from typing import Literal

BROWSER_ERROR_INVALID_REF = "invalid_ref"
BROWSER_ERROR_ACTION_FAILED = "action_failed"

BrowserErrorCode = Literal[
    "invalid_ref",
    "action_failed",
]


__all__ = [
    "BROWSER_ERROR_ACTION_FAILED",
    "BROWSER_ERROR_INVALID_REF",
    "BrowserErrorCode",
]
