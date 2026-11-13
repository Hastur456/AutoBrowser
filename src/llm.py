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
from typing import Any, Literal, Protocol, Sequence, get_args, runtime_checkable

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


#: What failed in a provider call, set by the adapter that saw the failure.
ProviderErrorKind = Literal[
    "connect",  # DNS, refused, reset, TLS, proxy: the request never reached the model
    "timeout",  # see ``TimeoutPhase``
    "rate_limited",  # 429
    "unavailable",  # 408, 409, 503, 529: refused before any processing
    "server_error",  # 500, 502, 504 and other 5xx: may have been processed
    "interrupted",  # the connection dropped after the request was sent
    "auth",  # 401, 403
    "payment",  # 402
    "not_found",  # 404: model or endpoint
    "bad_request",  # other 4xx
    "empty_response",  # a reply without a usable choice
    "malformed_response",  # a reply that did not parse
    "unexpected",  # anything else (and errors built without a kind)
]
#: The HTTP phase that ran out of time; ``deadline`` is the whole call's budget.
TimeoutPhase = Literal["connect", "write", "pool", "read", "deadline"]
#: Whether a repeat can generate twice: ``safe`` (the model never started), ``ambiguous``
#: (it may have), ``never`` (a repeat cannot succeed or was already paid for).
RetrySafety = Literal["safe", "ambiguous", "never"]
#: Why a connection failed, read from exception types and ``errno`` (never their text).
ConnectCause = Literal["dns", "refused", "reset", "tls", "proxy", "unreachable", "unknown"]

PROVIDER_ERROR_KINDS: frozenset[str] = frozenset(get_args(ProviderErrorKind))

_SAFE_KINDS = frozenset({"connect", "rate_limited", "unavailable"})
_AMBIGUOUS_KINDS = frozenset({"server_error", "interrupted"})
_SAFE_TIMEOUT_PHASES = frozenset({"connect", "write", "pool"})


@dataclass(frozen=True)
class ProviderAttempt:
    """One attempt of a model call; a failed one names how it failed."""

    attempt: int
    elapsed_ms: int
    #: The pause before this attempt.
    delay_ms: int = 0
    kind: str | None = None
    phase: str | None = None
    status_code: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class ModelCallInfo:
    """How one logical model call went: attempts, wall time, the provider's request id."""

    attempts: tuple[ProviderAttempt, ...] = ()
    elapsed_ms: int | None = None
    request_id: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        """Event fields: the attempt count, and the failed attempts when there were any."""

        payload: dict[str, Any] = {}
        if self.attempts:
            payload["attempts"] = len(self.attempts)
            failed = [attempt.to_json_dict() for attempt in self.attempts if attempt.kind]
            if failed:
                payload["failed_attempts"] = failed
        if self.elapsed_ms is not None:
            payload["elapsed_ms"] = self.elapsed_ms
        if self.request_id:
            payload["request_id"] = self.request_id
        return payload


#: ``ModelProviderError`` fields copied by :meth:`ModelProviderError.with_details`.
_ERROR_FIELDS = (
    "provider",
    "status_code",
    "kind",
    "phase",
    "cause",
    "retry_after_seconds",
    "request_id",
    "endpoint",
    "model",
    "attempts",
    "elapsed_ms",
)


class ModelProviderError(RuntimeError):
    """A provider call failed.

    The message is short, already truncated, and never carries a key or a header
    value, so it can reach events and the terminal as is. ``provider`` names the wire
    protocol (``settings.llm.provider``), not a vendor.

    The adapter that saw the failure classifies it: ``kind``, the timed-out ``phase``, the
    connection ``cause``, the provider's ``retry_after_seconds`` and ``request_id``. The
    retry layer adds ``attempts`` and ``elapsed_ms``. ``retry_safety`` follows from them;
    ``retryable`` (``retry_safety != "never"``) stays for callers that only want a yes/no.
    An error built without a ``kind`` keeps the ``retryable`` it was given.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool | None = None,
        kind: ProviderErrorKind = "unexpected",
        phase: TimeoutPhase | None = None,
        cause: ConnectCause | None = None,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        attempts: tuple[ProviderAttempt, ...] = (),
        elapsed_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.kind = kind
        self.phase = phase
        self.cause = cause
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id
        self.endpoint = endpoint
        self.model = model
        self.attempts = tuple(attempts)
        self.elapsed_ms = elapsed_ms
        self._retryable = retryable

    @property
    def retry_safety(self) -> RetrySafety:
        """Can the call be repeated without generating twice? See ``RetrySafety``."""

        if self.kind == "timeout":
            if self.phase in _SAFE_TIMEOUT_PHASES:
                return "safe"
            return "ambiguous" if self.phase in ("read", None) else "never"
        if self.kind in _SAFE_KINDS:
            return "safe"
        if self.kind in _AMBIGUOUS_KINDS:
            return "ambiguous"
        if self.kind == "unexpected" and self._retryable:
            return "ambiguous"
        return "never"

    @property
    def retryable(self) -> bool:
        if self.kind == "unexpected" and self._retryable is not None:
            return self._retryable
        return self.retry_safety != "never"

    def with_details(self, **changes: Any) -> ModelProviderError:
        """A copy of this error with some fields replaced (the message is kept)."""

        unknown = set(changes) - set(_ERROR_FIELDS)
        if unknown:
            raise TypeError(f"unknown ModelProviderError fields: {sorted(unknown)}")
        fields = {name: getattr(self, name) for name in _ERROR_FIELDS}
        fields.update(changes)
        return type(self)(str(self), retryable=self._retryable, **fields)

    def to_json_dict(self) -> dict[str, Any]:
        """The classification as event fields; ``None`` fields are left out."""

        payload: dict[str, Any] = {
            "provider": self.provider,
            "kind": self.kind,
            "retryable": self.retryable,
            "retry_safety": self.retry_safety,
        }
        optional = {
            "status_code": self.status_code,
            "phase": self.phase,
            "cause": self.cause,
            "retry_after_seconds": self.retry_after_seconds,
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.attempts:
            payload["attempts"] = [attempt.to_json_dict() for attempt in self.attempts]
        return payload

    def __reduce__(self) -> tuple[Any, ...]:
        # ``copy``/``pickle`` rebuild an exception from its positional ``args`` only, which
        # would drop the keyword-only fields (and fail on the required ``provider``). The
        # third item restores ``__dict__``, notes from ``add_note()`` included.
        return (_rebuild_provider_error, (type(self), str(self)), dict(self.__dict__))


def _rebuild_provider_error(cls: type[ModelProviderError], message: str) -> ModelProviderError:
    # The fields come back with ``__dict__`` (the third item of ``__reduce__``).
    return cls(message, provider="")


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
    #: Attempts, wall time and request id of the call, ``None`` when nobody recorded them.
    call: ModelCallInfo | None = None

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

    ``tiers[0]`` is the session model; a cascade (``cascade.*``) adds the tiers above it,
    built by the composition root. One tier is the behaviour without a cascade.
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
    "PROVIDER_ERROR_KINDS",
    "ChatModel",
    "ConnectCause",
    "ModelCallInfo",
    "ModelPrice",
    "ModelProviderError",
    "ModelResponse",
    "ModelTier",
    "ModelTiers",
    "ModelUsage",
    "ProviderAttempt",
    "ProviderErrorKind",
    "RetrySafety",
    "TimeoutPhase",
]
