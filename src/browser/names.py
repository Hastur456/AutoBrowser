"""Browser tool names the loop relies on.

The loop compares tool names exactly as the MCP bridge exposes them; the browser server is
exposed unprefixed (``MCPToolSource(unprefixed_servers=[browser server])``), so its tools keep
their own ``browser_*`` names. There is no second, canonical vocabulary to translate from.
"""

from __future__ import annotations

BROWSER_TOOL_PREFIX = "browser_"
SNAPSHOT_TOOL = "browser_snapshot"
TABS_TOOL = "browser_tabs"


def is_browser_tool_name(name: str) -> bool:
    """Return whether a name belongs to the browser server's tools."""

    return str(name or "").strip().startswith(BROWSER_TOOL_PREFIX)


def is_browser_snapshot_name(name: str) -> bool:
    """Return whether a name addresses the browser snapshot tool."""

    return str(name or "").strip() == SNAPSHOT_TOOL


__all__ = [
    "BROWSER_TOOL_PREFIX",
    "SNAPSHOT_TOOL",
    "TABS_TOOL",
    "is_browser_snapshot_name",
    "is_browser_tool_name",
]
