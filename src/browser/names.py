"""Shared browser tool-name helpers and mappings."""

from __future__ import annotations

CANONICAL_TO_PLAYWRIGHT = {
    "browser.navigate": "browser_navigate",
    "browser.snapshot": "browser_snapshot",
    "browser.click": "browser_click",
    "browser.type": "browser_type",
    "browser.hover": "browser_hover",
    "browser.evaluate": "browser_evaluate",
}

PLAYWRIGHT_TO_CANONICAL = {
    playwright_name: canonical_name
    for canonical_name, playwright_name in CANONICAL_TO_PLAYWRIGHT.items()
}


def to_playwright_browser_name(name: str) -> str:
    """Map a canonical browser tool name to the Playwright MCP tool name."""

    normalized = str(name or "").strip()
    return CANONICAL_TO_PLAYWRIGHT.get(normalized, normalized)


def to_canonical_browser_name(name: str) -> str:
    """Map a Playwright MCP browser tool name to the canonical browser name."""

    normalized = str(name or "").strip()
    return PLAYWRIGHT_TO_CANONICAL.get(normalized, normalized)


def is_browser_tool_name(name: str) -> bool:
    """Return whether a name belongs to the browser tool vocabulary."""

    normalized = str(name or "").strip()
    return (
        normalized in CANONICAL_TO_PLAYWRIGHT
        or normalized.startswith("browser.")
        or normalized.startswith("browser_")
    )


def is_browser_snapshot_name(name: str) -> bool:
    """Return whether a name addresses the browser snapshot tool."""

    normalized = str(name or "").strip()
    return normalized in {"browser.snapshot", "browser_snapshot"}


__all__ = [
    "CANONICAL_TO_PLAYWRIGHT",
    "PLAYWRIGHT_TO_CANONICAL",
    "is_browser_snapshot_name",
    "is_browser_tool_name",
    "to_canonical_browser_name",
    "to_playwright_browser_name",
]
