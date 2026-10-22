"""Neutral provider-agnostic chat contract.

The provider-neutral model contract the engine drives lives here: a
:class:`ChatModel` returns a :class:`ModelResponse` from a list of
:class:`~src.messages.Message`, optionally given tool schemas.

Model names and sampling defaults are **not** defined here — they are
configured in :mod:`src.config` (``settings.llm``).

A concrete provider (``src/providers/``: Ollama, any OpenAI-compatible service) is a
thin adapter that (a) serializes ``Message`` objects and ``ToolDef`` schemas into the
provider wire format and (b) parses the provider reply back into a
:class:`ModelResponse` with its :class:`ModelUsage`. A failed call surfaces as
:class:`ModelProviderError`. The engine never sees provider objects.

It imports nothing from ``src/agent/``, ``src/agent_loop/``, ``src/harness/`` or
``src/browser/``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from src.contracts import ToolDef
from src.messages import Message, ToolCall


@dataclass(frozen=True)
class ModelUsage:
    """Token accounting reported by the provider for one call.

    Every field is optional: ``None`` means "not reported", which is kept apart from a
    reported ``0``. Durations are milliseconds.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    provider_duration_ms: int | None = None
    load_duration_ms: int | None = None

    def to_json_dict(self) -> dict[str, int]:
        """The reported fields only, ready for an event payload."""

        return {key: value for key, value in asdict(self).items() if value is not None}


class ModelProviderError(RuntimeError):
    """A provider call failed.

    The message is short, already truncated, and never carries a key or a header
    value, so it can reach events and the terminal as is. ``provider`` names the wire
    protocol (``settings.llm.provider``), not a vendor; ``retryable`` marks transient
    failures (connection, timeout, 429, 5xx) for callers that want to retry.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


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
    #: Token accounting, ``None`` when the provider reported none.
    usage: ModelUsage | None = None

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
    "ModelProviderError",
    "ModelResponse",
    "ModelUsage",
]
