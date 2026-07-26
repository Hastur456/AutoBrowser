"""Shared browser-layer error codes."""

from __future__ import annotations

from typing import Literal

BROWSER_ERROR_INVALID_REF = "invalid_ref"
BROWSER_ERROR_UNKNOWN_ACTION = "unknown_action"
BROWSER_ERROR_ACTION_FAILED = "action_failed"

BrowserErrorCode = Literal[
    "invalid_ref",
    "unknown_action",
    "action_failed",
]


__all__ = [
    "BROWSER_ERROR_ACTION_FAILED",
    "BROWSER_ERROR_INVALID_REF",
    "BROWSER_ERROR_UNKNOWN_ACTION",
    "BrowserErrorCode",
]
