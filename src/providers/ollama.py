"""Native Ollama chat provider implementing the provider-neutral ``ChatModel``.

This adapter talks to a local Ollama server over ``/api/chat`` through the
``ollama`` Python client (already a dependency) and exposes the
:class:`~src.llm.ChatModel.complete` contract the engine drives. The ``ollama``
package is imported lazily so importing this module never requires Ollama tooling
unless a model is actually constructed.

Only wire concerns live here: neutral ``Message``/``ToolDef`` objects are serialized to
Ollama's request shape and the reply is parsed back into a :class:`~src.llm.ModelResponse`
with its :class:`~src.llm.ModelUsage`; client failures become
:class:`~src.llm.ModelProviderError`.

Connection settings come from :mod:`src.config` (``settings.llm``): ``host`` is handed to
the client as ``base_url``, ``timeout_seconds`` bounds one call, and a configured
``api_key`` becomes an explicit ``Authorization: Bearer`` header. Passing the header
ourselves (rather than relying on the client's own ``OLLAMA_API_KEY`` lookup) keeps every
credential in one configuration module; when no key is configured the client's discovery
is left untouched for local daemons.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import SecretStr

from src.config import LLMSettings, get_settings
from src.contracts import ToolDef
from src.llm import ChatModel, ModelProviderError, ModelResponse, ModelUsage
from src.messages import Message, ToolCall
from src.providers._wire import (
    arguments_to_dict,
    connection_error,
    interrupted_error,
    is_interrupted,
    new_call_id,
    optional_int,
    preview,
    public_url,
    status_error,
    timeout_error,
    timeout_phase,
    tool_to_wire,
)

#: Protocol name carried by this adapter's errors (``settings.llm.provider``).
PROVIDER = "ollama"

#: Call params forwarded into Ollama ``options``; anything else is ignored.
_OPTION_PARAMS = ("num_predict", "num_ctx", "top_p", "seed")

#: Ollama reports durations in nanoseconds.
_NS_PER_MS = 1_000_000


class OllamaChatModel(ChatModel):
    """One Ollama model behind the provider-neutral :class:`ChatModel` contract."""

    def __init__(
        self,
        model: str | None = None,
        temperature: float | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        *,
        settings: LLMSettings | None = None,
        client: Any | None = None,
    ) -> None:
        """Omitted ``model``/``temperature``/``base_url``/``timeout`` come from
        ``settings`` (``get_settings().llm`` when not given). A ``None`` temperature
        there sends none at all; a ``None`` ``timeout_seconds`` waits forever."""

        settings = settings if settings is not None else get_settings().llm
        self._model = model if model is not None else settings.model
        resolved = temperature if temperature is not None else settings.temperature
        self._temperature = float(resolved) if resolved is not None else None
        self._max_output_tokens = settings.max_output_tokens
        self._timeout = timeout if timeout is not None else settings.timeout_seconds
        self._phase_limits = phase_limits(settings, read=self._timeout)
        self._error_preview_chars = settings.error_preview_chars
        self._host = base_url if base_url is not None else settings.host
        if client is None:
            import httpx
            from ollama import AsyncClient  # lazy: only needed when a model is built

            client = AsyncClient(
                host=self._host,
                timeout=httpx.Timeout(
                    self._timeout,
                    connect=self._phase_limits["connect"],
                    write=self._phase_limits["write"],
                    pool=self._phase_limits["pool"],
                ),
                headers=auth_headers(settings.api_key),
            )
        self._client = client

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDef] = (),
        **params: Any,
    ) -> ModelResponse:
        """Return one model response for ``messages``, optionally binding tools."""

        request: dict[str, Any] = {
            "model": self._model,
            "messages": [_message_to_wire(message) for message in messages],
            "options": _options(self._temperature, self._max_output_tokens, params),
        }
        if tools:
            request["tools"] = [tool_to_wire(tool_def) for tool_def in tools]

        try:
            reply = await self._client.chat(**request)
        except ModelProviderError as exc:
            raise self._located(exc) from exc
        except Exception as exc:
            error = provider_error(
                exc, self._timeout, self._error_preview_chars, phase_limits=self._phase_limits
            )
            if error is None:
                raise
            raise self._located(error) from exc
        return _response_from_reply(reply)

    def _located(self, error: ModelProviderError) -> ModelProviderError:
        """``error`` with the endpoint (no credentials) and the model it concerns."""

        return error.with_details(endpoint=public_url(self._host), model=self._model)


def ollama_llm_factory(
    model: str | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> OllamaChatModel:
    """Build an :class:`OllamaChatModel` (signature-compatible with ``llm_factory``).

    Omitted ``model``/``temperature`` fall back to ``settings.llm``.
    """

    return OllamaChatModel(model=model, temperature=temperature, **kwargs)


def auth_headers(api_key: SecretStr | None) -> dict[str, str] | None:
    """Build the ``Authorization`` header for a configured API key.

    Returns ``None`` — not an empty mapping — when no usable key is configured,
    so a local Ollama daemon keeps the client's own credential discovery.
    """

    if api_key is None:
        return None
    key = api_key.get_secret_value().strip()
    if not key:
        return None
    return {"Authorization": f"Bearer {key}"}


def _options(
    temperature: float | None,
    max_output_tokens: int | None,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    for key in _OPTION_PARAMS:
        if key in params:
            options[key] = params[key]
    return options


def phase_limits(settings: LLMSettings, *, read: float | None) -> dict[str, float | None]:
    """The limit of every HTTP phase; ``read`` is the read timeout actually in use."""

    limits = {phase: settings.phase_timeout(phase) for phase in ("connect", "write", "pool")}
    if read != settings.timeout_seconds:
        # An explicit ``timeout`` argument stands in for ``timeout_seconds`` everywhere.
        for phase in ("connect", "write", "pool"):
            if getattr(settings, f"{phase}_timeout_seconds") is None:
                limits[phase] = read
    return {**limits, "read": read}


def provider_error(
    exc: Exception,
    timeout: float | None,
    limit: int,
    *,
    phase_limits: dict[str, float | None] | None = None,
) -> ModelProviderError | None:
    """Translate an ``ollama``/``httpx`` failure; ``None`` for anything else.

    ``timeout`` is the limit of every phase unless ``phase_limits`` names its own.
    """

    import httpx
    from ollama import RequestError, ResponseError

    if isinstance(exc, ResponseError):
        if exc.status_code and exc.status_code > 0:
            return status_error(PROVIDER, exc.status_code, exc.error, limit)
        return ModelProviderError(
            f"{PROVIDER}: {preview(exc.error, limit) or 'request failed'}",
            provider=PROVIDER,
        )
    if isinstance(exc, httpx.TimeoutException):
        phase = timeout_phase(exc)
        seconds = (phase_limits or {}).get(phase, timeout) if phase else timeout
        return timeout_error(PROVIDER, seconds, phase=phase)
    if isinstance(exc, httpx.TransportError) and is_interrupted(exc):
        return interrupted_error(PROVIDER, exc)
    if isinstance(exc, (ConnectionError, httpx.TransportError)):
        return connection_error(PROVIDER, exc)
    if isinstance(exc, RequestError):
        return ModelProviderError(
            f"{PROVIDER}: {preview(exc.error, limit) or 'invalid request'}",
            provider=PROVIDER,
            kind="bad_request",
        )
    return None


def _message_to_wire(message: Message) -> dict[str, Any]:
    """Serialize one neutral ``Message`` into the Ollama ``/api/chat`` shape."""

    if message.is_tool_call:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "function": {
                        "name": tool_call.name,
                        "arguments": dict(tool_call.arguments),
                    }
                }
                for tool_call in message.tool_calls
            ],
        }
    if message.role == "tool":
        wire: dict[str, Any] = {"role": "tool", "content": message.content}
        if message.name:
            wire["name"] = message.name
        if message.tool_call_id:
            wire["tool_call_id"] = message.tool_call_id
        return wire
    return {"role": message.role, "content": message.content}


def _response_from_reply(reply: Any) -> ModelResponse:
    """Parse an ``ollama`` chat reply into a :class:`ModelResponse`."""

    message = getattr(reply, "message", None) or {}
    content = str(getattr(message, "content", "") or "")
    tool_calls: list[ToolCall] = []
    for raw_call in getattr(message, "tool_calls", None) or []:
        function = getattr(raw_call, "function", raw_call)
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        tool_calls.append(
            ToolCall(
                id=new_call_id(),
                name=name,
                arguments=arguments_to_dict(getattr(function, "arguments", {})),
            )
        )
    return ModelResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=getattr(reply, "done_reason", None),
        usage=_usage_from_reply(reply),
    )


def _usage_from_reply(reply: Any) -> ModelUsage | None:
    """Token counts and durations of an Ollama reply; ``None`` when it reports none."""

    usage = ModelUsage(
        input_tokens=optional_int(getattr(reply, "prompt_eval_count", None)),
        output_tokens=optional_int(getattr(reply, "eval_count", None)),
        provider_duration_ms=_ns_to_ms(getattr(reply, "total_duration", None)),
        load_duration_ms=_ns_to_ms(getattr(reply, "load_duration", None)),
    )
    return usage if usage.to_json_dict() else None


def _ns_to_ms(value: Any) -> int | None:
    nanoseconds = optional_int(value)
    return nanoseconds // _NS_PER_MS if nanoseconds is not None else None


__all__ = ["OllamaChatModel", "auth_headers", "ollama_llm_factory", "provider_error"]
