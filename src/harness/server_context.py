"""Server context as prompt sections: what each MCP server says about its own tools.

A server may send ``instructions`` in its ``initialize`` result and may offer ``prompts``.
At session start the session turns them into fixed system blocks (:class:`PromptSection`), so
any server explains its tools to the agent without a rule in the code or the prompts:

- the server's ``instructions``, unless its ``mcp_servers`` entry sets ``use_instructions:
  false``;
- every prompt the entry names in ``context_prompts``, fetched once with ``prompts/get``. Only
  prompts without required arguments can be fetched this way; a missing, argument-taking or
  failing prompt is skipped with an ``mcp.context_skipped`` event and never fails startup.

Read once: a server that is not ready at session start (a failed non-browser server)
contributes only the instructions of an earlier connection, and a server that recovers later
adds nothing for the rest of the session (its tools still reach the model).

Each block is cut to the entry's ``context_max_chars``. Server-neutral: nothing here knows a
server, a tool or a prompt by name; the names come from the configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.agent_loop.context import PromptSection
from src.mcp import ConnectionState, MCPManager, PromptDescriptor

#: After the skill catalog (44) and the always-on skill bodies (45).
SERVER_CONTEXT_PRIORITY = 46
#: Block id prefix: ``mcp:<server>`` for instructions, ``mcp:<server>:<prompt>`` for a prompt.
SECTION_PREFIX = "mcp:"

ContextEvent = Callable[[str, dict[str, Any]], None]


async def server_sections(
    manager: MCPManager, *, on_event: ContextEvent | None = None
) -> list[PromptSection]:
    """The context blocks of every configured server, in server order."""

    prompts = {(p.server, p.local_name): p for p in manager.list_prompts()}
    states = {name: status.state for name, status in manager.status().items()}
    sections: list[PromptSection] = []
    for server in manager.servers():
        config = manager.server_config(server)
        limit = config.context_max_chars
        instructions = (manager.instructions(server) or "").strip()
        if config.use_instructions and instructions:
            sections.append(
                PromptSection(
                    name=f"{SECTION_PREFIX}{server}",
                    title=f"Server Instructions: {server}",
                    text=instructions[:limit],
                    priority=SERVER_CONTEXT_PRIORITY,
                )
            )
        for name in config.context_prompts:
            state = states.get(server)
            if state is not ConnectionState.READY:
                text, reason = "", f"the server is not ready ({state.value if state else 'unknown'})"
            else:
                text, reason = await _prompt_text(manager, prompts.get((server, name)))
            if reason:
                if on_event is not None:
                    on_event(
                        "mcp.context_skipped",
                        {"server": server, "prompt": name, "reason": reason},
                    )
                continue
            sections.append(
                PromptSection(
                    name=f"{SECTION_PREFIX}{server}:{name}",
                    title=f"Server Prompt: {server}/{name}",
                    text=text[:limit],
                    priority=SERVER_CONTEXT_PRIORITY,
                )
            )
    return sections


async def _prompt_text(
    manager: MCPManager, descriptor: PromptDescriptor | None
) -> tuple[str, str]:
    """``(text, "")`` for a fetched prompt, ``("", reason)`` when it cannot be used."""

    if descriptor is None:
        return "", "the server does not offer this prompt"
    if any(argument.get("required") for argument in descriptor.arguments):
        return "", "the prompt has required arguments"
    try:
        result = await manager.get_prompt(descriptor.qualified_name)
    except Exception as exc:  # noqa: BLE001 - a broken prompt never fails startup
        return "", f"prompts/get failed: {type(exc).__name__}"
    text = "\n\n".join(
        content.text.strip()
        for message in result.messages
        if (content := message.content) is not None and getattr(content, "type", "") == "text"
    ).strip()
    if not text:
        return "", "the prompt has no text"
    return text, ""


__all__ = ["SECTION_PREFIX", "SERVER_CONTEXT_PRIORITY", "ContextEvent", "server_sections"]
