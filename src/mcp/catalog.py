"""Connection states, discovered-catalog descriptors and discovery helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mcp import ClientSession, types


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class ToolDescriptor:
    server: str
    local_name: str  # the name the server uses
    input_schema: dict[str, Any]
    description: str | None
    title: str | None = None
    annotations: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    qualified_name: str = ""  # assigned when indexes are rebuilt — what the harness/LLM sees


@dataclass
class ResourceDescriptor:
    server: str
    uri: str
    name: str | None
    mime_type: str | None
    description: str | None = None


@dataclass
class ResourceTemplateDescriptor:
    server: str
    uri_template: str
    name: str | None
    mime_type: str | None
    description: str | None = None


@dataclass
class PromptDescriptor:
    server: str
    local_name: str
    description: str | None
    arguments: list[dict[str, Any]] = field(default_factory=list)
    qualified_name: str = ""


@dataclass(frozen=True)
class ServerStatus:
    state: ConnectionState
    generation: int
    last_error: str | None
    catalog_stale: bool
    reconnect_attempts: int
    stateful: bool
    connection_mode: str
    server_name: str | None = None  # serverInfo.name reported by the server
    server_version: str | None = None


@dataclass
class Catalog:
    """What one server returned via ``*/list``. Replaced atomically per primitive kind."""

    tools: dict[str, ToolDescriptor] | None = None
    resources: dict[str, ResourceDescriptor] | None = None
    resource_templates: list[ResourceTemplateDescriptor] | None = None
    prompts: dict[str, PromptDescriptor] | None = None


ALL_KINDS: frozenset[str] = frozenset({"tools", "resources", "prompts"})


async def _list_all(fetch: Callable[..., Awaitable[Any]], attr: str) -> list[Any]:
    """Follow ``nextCursor`` pagination to the end."""

    items: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = await fetch(cursor=cursor)
        items.extend(getattr(page, attr) or [])
        cursor = page.nextCursor
        if not cursor or cursor in seen:  # guard against a server looping on one cursor
            return items
        seen.add(cursor)


def _dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True, by_alias=True)
    return dict(value)


async def discover(
    server: str,
    session: ClientSession,
    capabilities: types.ServerCapabilities | None,
    kinds: frozenset[str] | set[str],
) -> Catalog:
    """Fetch the requested primitive lists — only those the server declared."""

    catalog = Catalog()
    caps = capabilities or types.ServerCapabilities()
    if "tools" in kinds and caps.tools is not None:
        catalog.tools = {
            tool.name: ToolDescriptor(
                server=server,
                local_name=tool.name,
                input_schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
                description=tool.description,
                title=getattr(tool, "title", None),
                annotations=_dump(getattr(tool, "annotations", None)),
                output_schema=getattr(tool, "outputSchema", None),
            )
            for tool in await _list_all(session.list_tools, "tools")
        }
    if "resources" in kinds and caps.resources is not None:
        catalog.resources = {
            str(res.uri): ResourceDescriptor(
                server=server,
                uri=str(res.uri),
                name=res.name,
                mime_type=res.mimeType,
                description=res.description,
            )
            for res in await _list_all(session.list_resources, "resources")
        }
        catalog.resource_templates = [
            ResourceTemplateDescriptor(
                server=server,
                uri_template=tpl.uriTemplate,
                name=tpl.name,
                mime_type=tpl.mimeType,
                description=tpl.description,
            )
            for tpl in await _list_all(session.list_resource_templates, "resourceTemplates")
        ]
    if "prompts" in kinds and caps.prompts is not None:
        catalog.prompts = {
            prompt.name: PromptDescriptor(
                server=server,
                local_name=prompt.name,
                description=prompt.description,
                arguments=[_dump(arg) or {} for arg in (prompt.arguments or [])],
            )
            for prompt in await _list_all(session.list_prompts, "prompts")
        }
    return catalog


__all__ = [
    "ALL_KINDS",
    "Catalog",
    "ConnectionState",
    "PromptDescriptor",
    "ResourceDescriptor",
    "ResourceTemplateDescriptor",
    "ServerStatus",
    "ToolDescriptor",
    "discover",
]
