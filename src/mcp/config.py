"""Declarative MCP server configuration and the static server registry.

This module is the ``ServerRegistry`` layer of the MCP Manager design: *what* servers exist
and *how* to reach them. It holds no connections. Runtime state lives in
:mod:`src.mcp.manager`; the discovered catalog lives in :mod:`src.mcp.catalog`.

Adding a transport = a new ``*ServerConfig`` variant in :data:`MCPServerConfig` plus one
factory in :mod:`src.mcp.transports`. No server (Playwright included) gets a special path:
the only thing a server may declare about itself is ``stateful: true``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from src.mcp.naming import validate_server_name

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_strict(value: str) -> str:
    """Replace ``${VAR}`` with the environment value; fail loudly on unset variables.

    pydantic-settings does not expand variables inside YAML/JSON values, and
    ``os.path.expandvars`` silently keeps unknown references — hence a strict version.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name} is not set")
        return os.environ[name]

    return _ENV_REF.sub(_replace, value)


class ReconnectPolicy(BaseModel):
    """Exponential-backoff reconnect policy for one server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    max_attempts: int = Field(default=5, ge=1)
    base_delay_s: float = Field(default=1.0, ge=0.0)
    max_delay_s: float = Field(default=30.0, ge=0.0)

    def delay_for(self, attempt: int) -> float:
        """Delay before reconnect ``attempt`` (1-based): ``base * 2**(n-1)`` capped at max."""

        return min(self.base_delay_s * (2 ** max(0, attempt - 1)), self.max_delay_s)


class _BaseServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_mode: Literal["persistent", "ephemeral"] = "persistent"
    # True -> a restart of the server loses its state (browser, pages, cookies).
    # The manager never hides such a reconnect from the harness (generation bump +
    # ServerConnectionLostError(stateful=True)).
    stateful: bool = False
    init_timeout_s: float = Field(default=30.0, gt=0)  # initialize + initial discovery
    request_timeout_s: float = Field(default=30.0, gt=0)  # list/read/get_prompt/ping
    call_timeout_s: float = Field(default=60.0, gt=0)  # tools/call
    ping_timeout_s: float = Field(default=5.0, gt=0)
    reconnect: ReconnectPolicy = Field(default_factory=ReconnectPolicy)


class StdioServerConfig(_BaseServerConfig):
    """A server spawned as a child process speaking JSON-RPC over stdin/stdout."""

    transport: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    # None -> the SDK uses its safe default environment; a dict is merged *over* it.
    env: dict[str, str] | None = None
    cwd: str | None = None

    @field_validator("env", mode="after")
    @classmethod
    def _expand_env(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return {key: expand_env_strict(item) for key, item in value.items()}


class StreamableHttpServerConfig(_BaseServerConfig):
    """A remote server reached over the Streamable HTTP transport."""

    transport: Literal["streamable_http"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    # Read timeout for the long-lived SSE stream of a streamable HTTP session.
    sse_read_timeout_s: float = Field(default=300.0, gt=0)

    @field_validator("url", mode="after")
    @classmethod
    def _expand_url(cls, value: str) -> str:
        return expand_env_strict(value)

    @field_validator("headers", mode="after")
    @classmethod
    def _expand_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: expand_env_strict(item) for key, item in value.items()}


# A real discriminated union on ``transport`` (without ``discriminator`` pydantic v2 would
# validate the Union in "smart" mode by trying every variant).
MCPServerConfig = Annotated[
    Union[StdioServerConfig, StreamableHttpServerConfig],
    Field(discriminator="transport"),
]

_SERVER_CONFIG_ADAPTER: TypeAdapter[Any] = TypeAdapter(MCPServerConfig)
_SERVER_MAP_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, MCPServerConfig])


def parse_server_config(raw: Mapping[str, Any] | BaseModel) -> StdioServerConfig | StreamableHttpServerConfig:
    """Validate one raw config mapping into a typed server config."""

    if isinstance(raw, (StdioServerConfig, StreamableHttpServerConfig)):
        return raw
    return _SERVER_CONFIG_ADAPTER.validate_python(raw)


def parse_server_configs(raw: Mapping[str, Any]) -> dict[str, StdioServerConfig | StreamableHttpServerConfig]:
    """Validate a ``{name: config}`` mapping (e.g. the ``mcp_servers`` YAML section)."""

    parsed = _SERVER_MAP_ADAPTER.validate_python(dict(raw))
    for name in parsed:
        validate_server_name(name)
    return parsed


class ServerRegistry:
    """Declarative source of truth: which servers exist and how to reach them.

    Contains no connections. ``MCPManager`` reads it on construction and keeps it in sync
    on ``add_server``/``remove_server``.
    """

    def __init__(self, servers: Mapping[str, Any] | None = None) -> None:
        self._servers: dict[str, StdioServerConfig | StreamableHttpServerConfig] = {}
        for name, config in (servers or {}).items():
            self.add(name, config)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ServerRegistry:
        return cls(parse_server_configs(raw or {}))

    def add(self, name: str, config: Any) -> None:
        validate_server_name(name)
        if name in self._servers:
            raise ValueError(f"MCP server {name!r} is already registered")
        self._servers[name] = parse_server_config(config)

    def remove(self, name: str) -> None:
        self._servers.pop(name, None)

    def get(self, name: str) -> StdioServerConfig | StreamableHttpServerConfig:
        return self._servers[name]

    def all(self) -> dict[str, StdioServerConfig | StreamableHttpServerConfig]:
        return dict(self._servers)

    def __contains__(self, name: object) -> bool:
        return name in self._servers

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._servers))

    def __len__(self) -> int:
        return len(self._servers)


__all__ = [
    "MCPServerConfig",
    "ReconnectPolicy",
    "ServerRegistry",
    "StdioServerConfig",
    "StreamableHttpServerConfig",
    "expand_env_strict",
    "parse_server_config",
    "parse_server_configs",
]
