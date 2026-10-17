"""Browser tool roles for the engine (:class:`~src.contracts.ToolRoles`).

The engine keeps the latest page snapshot and drops it after a browser action; which tools
are browser tools and which one takes the snapshot is browser knowledge, so it lives here
and reaches the engine through ``EngineResources.tool_roles``.
"""

from __future__ import annotations

from src.browser.names import is_browser_snapshot_name, is_browser_tool_name


class BrowserToolRoles:
    """The snapshot tool observes the page; every browser tool acts on it."""

    def is_observation(self, name: str) -> bool:
        return is_browser_snapshot_name(name)

    def is_stateful(self, name: str) -> bool:
        return is_browser_tool_name(name)


__all__ = ["BrowserToolRoles"]
