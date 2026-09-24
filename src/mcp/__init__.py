"""Universal MCP Manager (Host-side pool of MCP clients).

Every MCP server — Playwright included — is just an entry in ``mcp_servers``; nothing in
this package knows about any particular server. See
``docs/development/2026-09-24-mcp-manager-migration.md``.
"""

from src.mcp.catalog import (
    ConnectionState,
    PromptDescriptor,
    ResourceDescriptor,
    ResourceTemplateDescriptor,
    ServerStatus,
    ToolDescriptor,
)
from src.mcp.config import (
    MCPServerConfig,
    ReconnectPolicy,
    ServerRegistry,
    StdioServerConfig,
    StreamableHttpServerConfig,
    parse_server_config,
    parse_server_configs,
)
from src.mcp.errors import (
    AmbiguousResourceError,
    MCPManagerError,
    RequestTimeoutError,
    ServerClosedError,
    ServerConnectionLostError,
    ServerStateLostError,
    ServerUnavailableError,
    UnknownPromptError,
    UnknownResourceError,
    UnknownServerError,
    UnknownToolError,
)
from src.mcp.manager import MCPManager
from src.mcp.naming import MAX_TOOL_NAME, qualify, validate_server_name

__all__ = [
    "MAX_TOOL_NAME",
    "AmbiguousResourceError",
    "ConnectionState",
    "MCPManager",
    "MCPManagerError",
    "MCPServerConfig",
    "PromptDescriptor",
    "ReconnectPolicy",
    "RequestTimeoutError",
    "ResourceDescriptor",
    "ResourceTemplateDescriptor",
    "ServerClosedError",
    "ServerConnectionLostError",
    "ServerRegistry",
    "ServerStateLostError",
    "ServerStatus",
    "ServerUnavailableError",
    "StdioServerConfig",
    "StreamableHttpServerConfig",
    "ToolDescriptor",
    "UnknownPromptError",
    "UnknownResourceError",
    "UnknownServerError",
    "UnknownToolError",
    "parse_server_config",
    "parse_server_configs",
    "qualify",
    "validate_server_name",
]
