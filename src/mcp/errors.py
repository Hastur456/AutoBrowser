"""Typed MCP Manager errors.

Every error carries a stable ``error_code`` so the harness (``ToolBroker``) can put it into
``ToolResult.error_code`` without knowing the MCP layer. Classification (SDK 1.x):

* tool ran and failed — ``CallToolResult.isError = True`` — returned as-is (not an error here);
* protocol error (bad params, unknown prompt, ...) — ``McpError`` with a JSON-RPC code —
  re-raised unchanged;
* request timeout — ``McpError(code=408)`` — :class:`RequestTimeoutError`;
* transport dropped — ``McpError(CONNECTION_CLOSED=-32000, "Connection closed")`` or anyio
  ``ClosedResourceError``/``BrokenResourceError``/``EndOfStream`` —
  :class:`ServerConnectionLostError`;
* connection closed by the manager — the child request task is cancelled —
  :class:`ServerClosedError`.
"""

from __future__ import annotations


class MCPManagerError(Exception):
    """Base class for all MCP Manager errors."""

    error_code: str = "mcp_error"


class UnknownServerError(MCPManagerError, KeyError):
    error_code = "mcp_unknown_server"

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return Exception.__str__(self)


class UnknownToolError(MCPManagerError):
    error_code = "mcp_unknown_tool"


class UnknownPromptError(MCPManagerError):
    error_code = "mcp_unknown_prompt"


class UnknownResourceError(MCPManagerError):
    error_code = "mcp_unknown_resource"


class AmbiguousResourceError(MCPManagerError):
    error_code = "mcp_ambiguous_resource"

    def __init__(self, uri: str, servers: list[str]) -> None:
        super().__init__(f"resource {uri!r} is served by several servers {servers}; pass server=")
        self.uri = uri
        self.servers = list(servers)


class ServerUnavailableError(MCPManagerError):
    """The server could not be brought to READY (connect/initialize failed or timed out)."""

    error_code = "mcp_server_unavailable"

    def __init__(self, server: str, reason: str | None = None) -> None:
        message = f"MCP server {server!r} is unavailable"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)
        self.server = server
        self.reason = reason


class ServerClosedError(MCPManagerError):
    """The request was aborted because the manager closed the connection."""

    error_code = "mcp_server_closed"

    def __init__(self, server: str) -> None:
        super().__init__(f"MCP server {server!r} connection was closed by the manager")
        self.server = server


class RequestTimeoutError(MCPManagerError):
    """No response within the timeout. Whether the server performed the action is unknown."""

    error_code = "mcp_request_timeout"

    def __init__(self, server: str, detail: str | None = None) -> None:
        message = f"MCP server {server!r} did not respond in time"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.server = server


class ServerConnectionLostError(MCPManagerError):
    """The transport dropped during the request. Whether it executed is unknown.

    ``stateful=True``: after the reconnect this is a *new* server instance without the
    previous state (for Playwright MCP: a new, empty browser).
    """

    error_code = "mcp_connection_lost"

    def __init__(self, server: str, generation: int, stateful: bool) -> None:
        suffix = "; server state is lost" if stateful else ""
        super().__init__(f"MCP server {server!r}: connection lost (generation {generation}){suffix}")
        self.server = server
        self.generation = generation
        self.stateful = stateful


class ServerStateLostError(MCPManagerError):
    """The caller pinned a generation of a stateful server, but the server was restarted.

    Raised *before* sending the request, so the action was definitely not performed.
    """

    error_code = "mcp_server_state_lost"

    def __init__(self, server: str, expected_generation: int, actual_generation: int) -> None:
        super().__init__(
            f"MCP server {server!r} was restarted (expected generation {expected_generation}, "
            f"now {actual_generation}); its previous state is lost"
        )
        self.server = server
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation


__all__ = [
    "AmbiguousResourceError",
    "MCPManagerError",
    "RequestTimeoutError",
    "ServerClosedError",
    "ServerConnectionLostError",
    "ServerStateLostError",
    "ServerUnavailableError",
    "UnknownPromptError",
    "UnknownResourceError",
    "UnknownServerError",
    "UnknownToolError",
]
