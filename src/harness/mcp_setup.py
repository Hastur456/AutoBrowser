"""Session-level MCP wiring: settings -> MCPManager -> tool source + normalizers.

Replaces ``src/mcp/playwright_runtime.py`` (``load_browser_provider`` /
``close_mcp_session``). Nothing here is Playwright-specific except the *default* entry used
when the settings define no MCP servers at all; every server, the browser one included, is
just an entry in ``mcp_servers``.

Runtime placeholders. Server configs may reference values that are only known once the
session has started Chrome: ``{cdp_port}`` and ``{cdp_endpoint}``. They are substituted in
``command``/``args``/``env``/``cwd`` (stdio) and ``url``/``headers`` (HTTP). ``${VAR}``
(environment) is expanded earlier, at config validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.types import Implementation

from src.browser.normalization import BrowserToolNormalizer
from src.config import get_settings
from src.harness.mcp_tools import MCPToolSource
from src.harness.normalization import SchemaArgsNormalizer, ToolCallNormalizer
from src.mcp import (
    ConnectionState,
    MCPManager,
    ServerRegistry,
    ServerStatus,
    StdioServerConfig,
    StreamableHttpServerConfig,
    parse_server_configs,
)

DEFAULT_BROWSER_SERVER = "playwright"
# Pin this to the version previously launched by src/mcp/playwright_runtime.py.
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@latest"


def default_mcp_servers() -> dict[str, Any]:
    """Fallback when the settings define no ``mcp_servers``: Playwright MCP attached to the
    session's Chrome over CDP (the same topology as the old playwright runtime)."""

    return {
        DEFAULT_BROWSER_SERVER: {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", PLAYWRIGHT_MCP_PACKAGE, "--cdp-endpoint", "{cdp_endpoint}"],
            "stateful": True,
            "call_timeout_s": 120,
        }
    }


class BrowserServerUnavailableError(RuntimeError):
    """The configured browser MCP server could not be started."""

    def __init__(self, server: str, status: ServerStatus | None) -> None:
        reason = (status.last_error if status else None) or "not registered"
        super().__init__(f"Browser MCP server {server!r} failed to start: {reason}")
        self.server = server
        self.status = status


def runtime_values(cdp_port: int, *, host: str = "127.0.0.1") -> dict[str, str]:
    return {"cdp_port": str(cdp_port), "cdp_endpoint": f"http://{host}:{cdp_port}"}


def _render(text: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def render_runtime_placeholders(config: Any, values: Mapping[str, str]) -> Any:
    """Return a copy of a server config with ``{name}`` runtime placeholders filled."""

    if isinstance(config, StdioServerConfig):
        return config.model_copy(
            update={
                "command": _render(config.command, values),
                "args": [_render(arg, values) for arg in config.args],
                "env": (
                    None
                    if config.env is None
                    else {key: _render(item, values) for key, item in config.env.items()}
                ),
                "cwd": None if config.cwd is None else _render(config.cwd, values),
            }
        )
    if isinstance(config, StreamableHttpServerConfig):
        return config.model_copy(
            update={
                "url": _render(config.url, values),
                "headers": {key: _render(item, values) for key, item in config.headers.items()},
            }
        )
    return config


@dataclass
class MCPRuntime:
    """Everything the session needs from MCP: the manager, the live tool source for the
    registry, the normalizers for the broker, and which server is the browser."""

    manager: MCPManager
    tool_source: MCPToolSource
    browser_server: str | None
    normalizers: list[ToolCallNormalizer] = field(default_factory=list)

    async def start(self, *, require_browser: bool = True) -> None:
        """Connect all servers. A failing non-browser server only shows up in ``status``;
        a failing browser server aborts the start (the agent cannot work without it)."""

        await self.manager.start()
        if not require_browser or self.browser_server is None:
            return
        status = self.manager.status().get(self.browser_server)
        if status is None or status.state is not ConnectionState.READY:
            await self.manager.shutdown()
            raise BrowserServerUnavailableError(self.browser_server, status)

    async def close(self) -> None:
        await self.manager.shutdown()

    def status(self) -> dict[str, ServerStatus]:
        return self.manager.status()


def build_mcp_runtime(
    *,
    cdp_port: int,
    settings: Any | None = None,
    client_version: str = "0",
) -> MCPRuntime:
    """Build (not start) the session's MCP runtime from settings.

    Reads ``settings.mcp_servers`` (falls back to :func:`default_mcp_servers`) and
    ``settings.browser_mcp_server`` (falls back to ``"playwright"`` when that server exists).
    """

    settings = settings if settings is not None else get_settings()
    raw_servers = getattr(settings, "mcp_servers", None) or default_mcp_servers()
    servers = parse_server_configs(raw_servers)
    values = runtime_values(cdp_port)
    registry = ServerRegistry(
        {name: render_runtime_placeholders(config, values) for name, config in servers.items()}
    )

    browser_server = getattr(settings, "browser_mcp_server", None)
    if browser_server is None and DEFAULT_BROWSER_SERVER in registry:
        browser_server = DEFAULT_BROWSER_SERVER
    if browser_server is not None and browser_server not in registry:
        raise ValueError(
            f"browser_mcp_server={browser_server!r} is not defined in mcp_servers "
            f"({', '.join(registry) or 'none'})"
        )

    manager = MCPManager(
        registry,
        client_info=Implementation(name="autobrowser", version=client_version),
    )
    tool_source = MCPToolSource(
        manager,
        unprefixed_servers=[browser_server] if browser_server else [],
    )
    return MCPRuntime(
        manager=manager,
        tool_source=tool_source,
        browser_server=browser_server,
        # Drop browser arguments the exposed tool's schema forbids before the
        # schema-based arg filter runs.
        normalizers=[BrowserToolNormalizer(), SchemaArgsNormalizer()],
    )


__all__ = [
    "DEFAULT_BROWSER_SERVER",
    "PLAYWRIGHT_MCP_PACKAGE",
    "BrowserServerUnavailableError",
    "MCPRuntime",
    "build_mcp_runtime",
    "default_mcp_servers",
    "render_runtime_placeholders",
    "runtime_values",
]