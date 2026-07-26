"""Browser backend adapters."""

from __future__ import annotations

from src.browser.names import (
    CANONICAL_TO_PLAYWRIGHT,
    PLAYWRIGHT_TO_CANONICAL,
    is_browser_snapshot_name,
    is_browser_tool_name,
    to_canonical_browser_name,
    to_playwright_browser_name,
)
from src.browser.adapters.playwright_mcp import (
    PlaywrightMCPBrowserProvider,
    element_description_from_snapshot,
)

__all__ = [
    "CANONICAL_TO_PLAYWRIGHT",
    "PLAYWRIGHT_TO_CANONICAL",
    "PlaywrightMCPBrowserProvider",
    "element_description_from_snapshot",
    "is_browser_snapshot_name",
    "is_browser_tool_name",
    "to_canonical_browser_name",
    "to_playwright_browser_name",
]
