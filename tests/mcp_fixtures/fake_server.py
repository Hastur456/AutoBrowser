"""Tiny stdio MCP server used by the MCP Manager tests (no network, no browser).

Behaviour switches mirror the failure modes the manager must handle: a stateful counter
(state loss on restart), a slow tool (timeouts), a crashing tool (transport drop during a
call), a delayed silent exit (death with nothing in flight), runtime tool registration with
``notifications/tools/list_changed``, a dotted tool name (name sanitizing), a resource and a
prompt.

Annotations: ``get.user`` is read-only, ``increment`` a non-destructive mutation and
``echo`` is unannotated (a server without annotations).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

server = FastMCP(
    os.environ.get("FAKE_SERVER_NAME", "fake"),
    log_level="WARNING",
    port=int(os.environ.get("FAKE_SERVER_PORT", "8000")),
)
_state = {"counter": 0}


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def increment() -> str:
    """Increment an in-process counter (server-side state)."""
    _state["counter"] += 1
    return str(_state["counter"])


@server.tool()
async def slow(seconds: float) -> str:
    """Sleep, then answer."""
    await asyncio.sleep(seconds)
    return "slept"


@server.tool()
def fail(message: str) -> str:
    """Raise -> CallToolResult.isError = True."""
    raise ValueError(message)


@server.tool()
def crash() -> str:
    """Kill the process while the call is in flight."""
    sys.stdout.flush()
    os._exit(3)


@server.tool()
def exit_later(delay: float = 0.2) -> str:
    """Answer, then die silently a bit later (nothing in flight)."""
    threading.Timer(delay, lambda: os._exit(4)).start()
    return "bye"


@server.tool()
async def add_tool(name: str, ctx: Context) -> str:
    """Register a new tool at runtime and send tools/list_changed."""

    def dynamic() -> str:
        return f"dynamic:{name}"

    server.add_tool(dynamic, name=name, description="added at runtime")
    await ctx.session.send_tool_list_changed()
    return "added"


@server.tool(name="get.user", annotations=ToolAnnotations(readOnlyHint=True))
def get_user_dotted() -> str:
    """A tool whose MCP name is not provider-safe."""
    return "dotted"


@server.resource("memo://notes")
def notes() -> str:
    return "hello notes"


@server.prompt()
def greet(who: str) -> str:
    return f"Say hello to {who}"


if __name__ == "__main__":
    server.run(os.environ.get("FAKE_SERVER_TRANSPORT", "stdio"))  # or "streamable-http"
