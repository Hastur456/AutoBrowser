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

    def __reduce__(self) -> tuple[Any, ...]:
        # ``copy``/``pickle`` rebuild an exception from its positional ``args`` only, which
        # would drop the keyword-only fields (and fail on the required ``provider``). The
        # third item restores ``__dict__``, notes from ``add_note()`` included.
        return (
            _rebuild_provider_error,
            (type(self), str(self), self.provider, self.status_code, self.retryable),
            dict(self.__dict__),
        )


def _rebuild_provider_error(
    cls: type[ModelProviderError],
    message: str,
    provider: str,
    status_code: int | None,
    retryable: bool,
) -> ModelProviderError:
    return cls(message, provider=provider, status_code=status_code, retryable=retryable)


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


#: Tokens per price unit: prices are quoted per million tokens.
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """What one million tokens of a model cost, in ``llm.currency``.

    ``input_tokens`` is taken to include cached input tokens (the OpenAI-compatible
    convention); with ``cached_input_per_mtok`` set, the cached part is charged at that rate.
    """

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None

    def cost(self, usage: ModelUsage | None) -> float | None:
        """The price of one call; ``None`` when the provider did not report its tokens."""

        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            return None
        cached = usage.cached_input_tokens or 0
        cached_rate = self.input_per_mtok
        if self.cached_input_per_mtok is not None:
            cached_rate = self.cached_input_per_mtok
        cached = min(cached, usage.input_tokens)
        total = (
            (usage.input_tokens - cached) * self.input_per_mtok
            + cached * cached_rate
            + usage.output_tokens * self.output_per_mtok
        )
        return total / TOKENS_PER_PRICE_UNIT


@dataclass(frozen=True)
class ModelTier:
    """One model the engine may drive, with the label and price reports use."""

    model: ChatModel
    #: The model id, for events and reports; ``""`` when unknown.
    label: str = ""
    price: ModelPrice | None = None


@dataclass(frozen=True)
class ModelTiers:
    """The models of a session; ``tiers[0]`` is the default and escalation only moves up.

    One tier is today's behaviour (the cascade of several tiers is a later phase).
    """

    tiers: tuple[ModelTier, ...]
    #: The tier a playback retarget uses.
    retarget_tier: int = 0

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("ModelTiers needs at least one tier.")
        if not 0 <= self.retarget_tier < len(self.tiers):
            raise ValueError("ModelTiers.retarget_tier is out of range.")

    @classmethod
    def single(
        cls, model: ChatModel, *, label: str = "", price: ModelPrice | None = None
    ) -> ModelTiers:
        return cls((ModelTier(model, label, price),))

    def tier(self, index: int) -> ModelTier:
        """The tier at ``index``, clamped to the configured ones."""

        return self.tiers[max(0, min(index, len(self.tiers) - 1))]


__all__ = [
    "ChatModel",
    "ModelPrice",
    "ModelProviderError",
    "ModelResponse",
    "ModelTier",
    "ModelTiers",
    "ModelUsage",
]
