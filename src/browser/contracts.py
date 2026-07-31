"""Neutral browser action and result contracts."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


BrowserActionName = Literal[
    "browser.navigate",
    "browser.snapshot",
    "browser.click",
    "browser.type",
    "browser.hover",
    "browser.evaluate",
    "browser.tabs",
]


class BrowserAction(TypedDict, total=False):
    """Provider-neutral browser action request."""

    name: BrowserActionName
    args: dict[str, Any]
    reason: str
    id: str


class BrowserResult(TypedDict, total=False):
    """Provider-neutral browser action result."""

    name: str
    status: Literal["success", "error"]
    content: str
    error: str
    error_code: str


__all__ = ["BrowserAction", "BrowserActionName", "BrowserResult"]
