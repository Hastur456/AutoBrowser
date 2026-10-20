"""Provider-neutral chat message model for the AutoBrowser agent harness.

This module is the single, dependency-free home for the message types the harness
passes to a reasoning model. The shape follows the conventions shared by
OpenAI-compatible chat endpoints (which Ollama implements at
``/v1/chat/completions``) and by the wider agent-harness ecosystem:

- four roles: ``system`` / ``user`` / ``assistant`` / ``tool``;
- an assistant message may carry ``tool_calls``; each call has a stable ``id``;
- a tool result is its own ``tool`` message that pairs back to exactly one
  ``ToolCall.id`` via ``tool_call_id`` — exact matching is what lets the model
  resume after a completed turn;
- history is replayed in full, in chronological order, keeping every
  ``tool_call`` -> ``tool`` result pair; the harness owns that list.

The harness executes tools and owns permissions; the model only proposes. These
types import nothing but the standard library, so both the engine
(``src/agent_loop/``) and the harness (``src/harness/``) layers can depend on
them without a provider or a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MessageRole = Literal["system", "user", "assistant", "tool"]

__all__ = [
    "Message",
    "MessageRole",
    "ToolCall",
    "assistant_message",
    "message_from_dict",
    "message_to_dict",
    "system_message",
    "tool_message",
    "user_message",
]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation proposed by the model (``assistant`` role)."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the OpenAI-compatible wire shape for this call."""

        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Message:
    """One provider-neutral chat message.

    ``role`` is the canonical wire role. ``tool_calls`` is populated only for
    ``assistant`` messages that propose tool use; ``tool_call_id`` is populated
    only for ``tool`` messages that carry a result back for one call; ``name``
    records the tool/function name where the role uses one. All fields are
    optional so builders stay terse and round-tripping is lossless.
    """

    role: MessageRole
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def is_tool_call(self) -> bool:
        """True when this assistant message proposes one or more tool calls."""

        return self.role == "assistant" and bool(self.tool_calls)

    @property
    def is_tool_result(self) -> bool:
        """True when this message carries a result for a prior tool call."""

        return self.role == "tool"


def system_message(content: str) -> Message:
    """Build a ``system`` message carrying the system prompt."""

    return Message(role="system", content=content)


def user_message(content: str) -> Message:
    """Build a ``user`` message carrying a user task or request."""

    return Message(role="user", content=content)


def assistant_message(
    content: str = "",
    *,
    tool_calls: tuple[ToolCall, ...] = (),
) -> Message:
    """Build an ``assistant`` message, optionally proposing tool calls."""

    return Message(role="assistant", content=content, tool_calls=tool_calls)


def tool_message(
    tool_call_id: str,
    content: str,
    *,
    name: str | None = None,
) -> Message:
    """Build a ``tool`` message pairing a result back to ``tool_call_id``.

    The id must exactly match the ``ToolCall.id`` of the assistant message that
    proposed the call, or the model will treat the result as unrelated.
    """

    return Message(role="tool", content=content, name=name, tool_call_id=tool_call_id)


def message_to_dict(message: Message) -> dict[str, Any]:
    """A lossless plain-JSON form of ``message`` (for ``state.json``)."""

    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name is not None:
        data["name"] = message.name
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        data["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    return data


def message_from_dict(data: dict[str, Any]) -> Message:
    """The inverse of :func:`message_to_dict`; raises ``ValueError`` on an unknown role."""

    role = data.get("role")
    if role not in ("system", "user", "assistant", "tool"):
        raise ValueError(f"Unknown message role: {role!r}")
    calls = tuple(
        ToolCall(
            id=str(call.get("id") or ""),
            name=str(call.get("name") or ""),
            arguments=dict(call.get("arguments") or {}),
        )
        for call in data.get("tool_calls") or ()
        if isinstance(call, dict)
    )
    return Message(
        role=role,
        content=str(data.get("content") or ""),
        name=data.get("name"),
        tool_call_id=data.get("tool_call_id"),
        tool_calls=calls,
    )
