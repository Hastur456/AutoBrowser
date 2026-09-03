"""Neutral model/LLM defaults and provider-agnostic chat contract.

``DEFAULT_OLLAMA_MODEL`` and the provider-neutral model contract the engine
drives live here: a :class:`ChatModel` returns a :class:`ModelResponse`
from a list of :class:`~src.messages.Message`, optionally given tool schemas.

A concrete provider (e.g. Ollama) is a thin adapter that (a) serializes
``Message`` objects and ``ToolDef`` schemas into the provider wire format and
(b) parses the provider reply back into a :class:`ModelResponse`. The engine
never sees provider objects.

It imports nothing from ``src/agent/``, ``src/agent_loop/``, ``src/harness/`` or
``src/browser/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from src.contracts import ToolDef
from src.messages import Message, ToolCall

DEFAULT_OLLAMA_MODEL = "gpt-oss:20b-cloud"


@dataclass(frozen=True)
class ModelResponse:
    """One canonical model reply, independent of the provider that produced it.

    Either ``content`` (a final answer / plain text) or ``tool_calls`` (a
    proposed tool invocation) is populated; both can appear together, matching
    providers that return text plus tool calls.
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """True when the model proposed at least one tool call."""

        return bool(self.tool_calls)


@runtime_checkable
class ChatModel(Protocol):
    """Provider-neutral chat model the engine drives.

    Implementations own wire serialization only: map ``messages`` and ``tools``
    to the provider API and parse the response into a :class:`ModelResponse`.
    They never make policy or execution decisions.
    """

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDef] = (),
        **params: Any,
    ) -> ModelResponse:
        """Return one model response for ``messages``, optionally binding tools."""
        ...


__all__ = [
    "ChatModel",
    "DEFAULT_OLLAMA_MODEL",
    "ModelResponse",
]
