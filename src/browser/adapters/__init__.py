"""Browser backend adapters."""

from __future__ import annotations

from src.browser.adapters.playwright_mcp import (
    PlaywrightMCPBrowserProvider,
    element_description_from_snapshot,
)

__all__ = ["PlaywrightMCPBrowserProvider", "element_description_from_snapshot"]
