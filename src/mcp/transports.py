"""Transport factories keyed by ``config.transport`` — no per-server special cases.

Each factory returns an async context manager yielding ``(read_stream, write_stream, ...)``
(``stdio_client`` yields two items, streamable HTTP three), hence ``read, write, *_``.
The legacy SSE transport is deprecated by the spec since 2025-03-26 and not wired.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, AsyncContextManager

import httpx
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from src.mcp.config import StdioServerConfig, StreamableHttpServerConfig

try:  # mcp >= 1.24: explicit httpx client; ``headers=`` on the old helper is deprecated
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client
except ImportError:  # pragma: no cover - older 1.x
    _streamable_http_client = None
from mcp.client.streamable_http import streamablehttp_client as _legacy_streamablehttp_client

try:
    from mcp.shared._httpx_utils import create_mcp_http_client
except ImportError:  # pragma: no cover
    create_mcp_http_client = None


def open_stdio_transport(config: StdioServerConfig) -> AsyncContextManager[Any]:
    # ``env`` is merged by the SDK over ``get_default_environment()``; None -> defaults only.
    # On exit ``stdio_client`` closes stdin, waits, then escalates to SIGTERM/SIGKILL of
    # the whole process tree — also when the owning task is cancelled.
    return stdio_client(
        StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=dict(config.env) if config.env is not None else None,
            cwd=config.cwd,
        )
    )


@contextlib.asynccontextmanager
async def _open_streamable_http(config: StreamableHttpServerConfig) -> AsyncIterator[Any]:
    timeout = httpx.Timeout(config.request_timeout_s, read=config.sse_read_timeout_s)
    if _streamable_http_client is not None and create_mcp_http_client is not None:
        client = create_mcp_http_client(headers=dict(config.headers), timeout=timeout)
        async with client:
            async with _streamable_http_client(config.url, http_client=client) as streams:
                yield streams
        return
    async with _legacy_streamablehttp_client(  # pragma: no cover - older 1.x
        config.url,
        headers=dict(config.headers),
        timeout=config.request_timeout_s,
        sse_read_timeout=config.sse_read_timeout_s,
    ) as streams:
        yield streams


def open_streamable_http_transport(config: StreamableHttpServerConfig) -> AsyncContextManager[Any]:
    return _open_streamable_http(config)


TRANSPORT_FACTORIES: dict[str, Callable[[Any], AsyncContextManager[Any]]] = {
    "stdio": open_stdio_transport,
    "streamable_http": open_streamable_http_transport,
}


def open_transport(config: Any) -> AsyncContextManager[Any]:
    try:
        factory = TRANSPORT_FACTORIES[config.transport]
    except KeyError:
        raise ValueError(f"unsupported MCP transport {config.transport!r}") from None
    return factory(config)


__all__ = [
    "TRANSPORT_FACTORIES",
    "open_stdio_transport",
    "open_streamable_http_transport",
    "open_transport",
]
