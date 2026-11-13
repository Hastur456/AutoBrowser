"""OpenAI-compatible chat provider implementing the provider-neutral ``ChatModel``.

One adapter per protocol, not per vendor: anything that speaks Chat Completions
(``POST {base_url}/chat/completions``) is reached by setting ``llm.host``, ``llm.api_key``
and ``llm.model`` -- no vendor name or URL lives in this module
(``docs/decisions/2026-10-22-openai-compatible-chat-provider.md``). The ``openai`` SDK is
the transport and is imported lazily, so importing this module needs nothing installed.

Only wire concerns live here: neutral ``Message``/``ToolDef`` objects are serialized to the
Chat Completions shape, the reply is parsed back into a :class:`~src.llm.ModelResponse`
with its :class:`~src.llm.ModelUsage`, and SDK failures become
:class:`~src.llm.ModelProviderError`.

Credentials come from ``settings.llm`` only. The SDK would otherwise read ``OPENAI_API_KEY``,
``OPENAI_BASE_URL``, ``OPENAI_ORG_ID``, ``OPENAI_PROJECT_ID``, ``OPENAI_ADMIN_KEY`` and
``OPENAI_CUSTOM_HEADERS`` from the environment and send keys, organization or headers of
another project to a third-party service; :func:`build_client` neutralizes all of them.
Without a key no ``Authorization`` header is sent (local servers).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from src.config import LLMSettings, get_settings
from src.contracts import ToolDef
from src.llm import ChatModel, ModelCallInfo, ModelProviderError, ModelResponse, ModelUsage
from src.messages import Message, ToolCall
from src.providers._wire import (
    arguments_to_dict,
    connection_error,
    empty_response_error,
    interrupted_error,
    is_interrupted,
    new_call_id,
    optional_int,
    preview,
    public_url,
    safe_request_id,
    status_error,
    timeout_error,
    timeout_phase,
    tool_to_wire,
)

#: Protocol name carried by this adapter's errors (``settings.llm.provider``).
PROVIDER = "openai_compat"

#: Call params forwarded as standard request fields; anything else is ignored.
_FORWARDED_PARAMS = ("top_p", "seed")

#: Stand-in key for a server that needs none: the SDK refuses to build a client without
#: one, and the ``Authorization`` header it would produce is omitted on every request.
_NO_KEY_PLACEHOLDER = "no-key"

#: Env var the SDK parses into default headers (``Name: value`` per line).
_SDK_CUSTOM_HEADERS_ENV = "OPENAI_CUSTOM_HEADERS"

_AUTHORIZATION = "Authorization"


class OpenAICompatChatModel(ChatModel):
    """One Chat Completions model behind the provider-neutral :class:`ChatModel` contract."""

    def __init__(
        self,
        model: str | None = None,
        temperature: float | None = None,
        *,
        settings: LLMSettings | None = None,
        client: Any | None = None,
    ) -> None:
        """Omitted ``model``/``temperature`` come from ``settings`` (``get_settings().llm``
        when not given); a ``None`` temperature there sends none at all. ``client`` lets
        tests inject a fake or a :func:`build_client` over a mock transport."""

        settings = settings if settings is not None else get_settings().llm
        self._settings = settings
        self._model = model if model is not None else settings.model
        resolved = temperature if temperature is not None else settings.temperature
        self._temperature = float(resolved) if resolved is not None else None
        self._client = client if client is not None else build_client(settings)
        self._request_headers = request_headers(settings)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDef] = (),
        **params: Any,
    ) -> ModelResponse:
        """Return one model response for ``messages``, optionally binding tools."""

        request = self._request(messages, tools, params)
        try:
            reply = await self._client.chat.completions.create(**request)
        except ModelProviderError as exc:
            raise self._located(exc) from exc
        except Exception as exc:
            error = provider_error(exc, self._settings)
            if error is None:
                raise
            raise self._located(error) from exc
        try:
            response = _response_from_reply(reply)
        except ModelProviderError as exc:
            raise self._located(exc) from exc
        rid = safe_request_id(getattr(reply, "_request_id", None))
        return replace(response, call=ModelCallInfo(request_id=rid)) if rid else response

    def _located(self, error: ModelProviderError) -> ModelProviderError:
        """``error`` with the endpoint (no credentials) and the model it concerns."""

        return error.with_details(endpoint=public_url(self._settings.host), model=self._model)

    def _request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        settings = self._settings
        request: dict[str, Any] = {
            "model": self._model,
            "messages": messages_to_wire(messages),
        }
        if tools:
            request["tools"] = [tool_to_wire(tool_def) for tool_def in tools]
            # OpenAI rejects ``parallel_tool_calls`` on a request without tools.
            if settings.parallel_tool_calls is not None:
                request["parallel_tool_calls"] = settings.parallel_tool_calls
        if self._temperature is not None:
            request["temperature"] = self._temperature
        if settings.max_output_tokens is not None:
            request[settings.max_tokens_param] = settings.max_output_tokens
        if settings.reasoning_effort is not None:
            request["reasoning_effort"] = settings.reasoning_effort
        for key in _FORWARDED_PARAMS:
            if key in params:
                request[key] = params[key]
        if settings.extra_body:
            request["extra_body"] = dict(settings.extra_body)
        if self._request_headers:
            request["extra_headers"] = dict(self._request_headers)
        return request


def openai_compat_llm_factory(
    model: str | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> OpenAICompatChatModel:
    """Build an :class:`OpenAICompatChatModel` (signature-compatible with ``llm_factory``).

    Omitted ``model``/``temperature`` fall back to ``settings.llm``.
    """

    return OpenAICompatChatModel(model=model, temperature=temperature, **kwargs)


def build_client(
    settings: LLMSettings,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
    http_client: Any | None = None,
) -> Any:
    """An ``AsyncOpenAI`` client configured from ``settings`` alone.

    The client gets one limit per HTTP phase (:meth:`LLMSettings.phase_timeout`) and never
    retries by itself: retries belong to :class:`~src.providers.resilience.ResilientChatModel`,
    which knows which failures are safe to repeat. ``timeout`` (one limit for every phase)
    and ``max_retries`` override that (the ``doctor --llm`` probe uses its own budget);
    ``http_client`` lets tests plug in a mock transport.
    ``Authorization`` comes from ``llm.extra_headers`` if set there, else from the key,
    else none is sent -- pair every request with :func:`request_headers` for that case.
    """

    if not (settings.host or "").strip():
        # The SDK would fall back to OPENAI_BASE_URL and then to a vendor URL.
        raise ValueError(
            "llm.host is required for provider openai_compat: set the service base URL, "
            "e.g. https://<service>/v1 (AUTOBROWSER_LLM__HOST)."
        )
    from openai import AsyncOpenAI, Omit  # lazy: only needed when a model is built

    key = _configured_key(settings)
    headers: dict[str, Any] = {
        name: value
        for name, value in settings.extra_headers.items()
        if name.lower() != _AUTHORIZATION.lower()
    }
    user_authorization = _user_authorization(settings)
    if user_authorization is not None:
        headers[_AUTHORIZATION] = user_authorization
    elif key:
        # Explicit, so an ``Authorization`` from OPENAI_CUSTOM_HEADERS cannot displace it.
        headers[_AUTHORIZATION] = f"Bearer {key}"
    else:
        headers[_AUTHORIZATION] = Omit()
    for name in _sdk_env_header_names():
        if name not in headers:  # exact spelling: the SDK merges these case-sensitively
            headers[name] = Omit()
    client = AsyncOpenAI(
        api_key=key or _NO_KEY_PLACEHOLDER,
        base_url=settings.host,
        timeout=timeout if timeout is not None else http_timeout(settings),
        max_retries=max_retries if max_retries is not None else 0,
        default_headers=headers,
        http_client=http_client,
    )
    # Read from the environment when not given; ``None`` sends no such header.
    client.organization = None
    client.project = None
    client.admin_api_key = None
    client.webhook_secret = None
    return client


def request_headers(settings: LLMSettings) -> dict[str, Any]:
    """Per-request headers: an omitted ``Authorization`` when there is nothing to send.

    The omission has to be per request too: the SDK checks each request for either an
    ``Authorization`` header or an explicit per-request omission of it.
    """

    if _configured_key(settings) or _user_authorization(settings) is not None:
        return {}
    from openai import Omit

    return {_AUTHORIZATION: Omit()}


def _configured_key(settings: LLMSettings) -> str:
    return settings.api_key.get_secret_value().strip() if settings.api_key else ""


def _user_authorization(settings: LLMSettings) -> str | None:
    """A non-empty ``Authorization`` set in ``llm.extra_headers`` (any spelling); it wins
    over the key. An empty one counts as unset rather than blanking the key."""

    for name, value in settings.extra_headers.items():
        if name.lower() == _AUTHORIZATION.lower() and value.strip():
            return value
    return None


def http_timeout(settings: LLMSettings) -> Any:
    """An ``httpx.Timeout`` with the read limit and one limit per other phase."""

    import httpx

    return httpx.Timeout(
        settings.timeout_seconds,
        connect=settings.phase_timeout("connect"),
        write=settings.phase_timeout("write"),
        pool=settings.phase_timeout("pool"),
    )


def provider_error(exc: BaseException, settings: LLMSettings) -> ModelProviderError | None:
    """Translate an ``openai`` SDK failure; ``None`` for anything else.

    The SDK wraps every transport failure in ``APIConnectionError`` (a timeout in its
    ``APITimeoutError`` subclass); the ``httpx`` cause underneath tells the phase, a
    connection that never opened from a response cut short.
    """

    import openai

    limit = settings.error_preview_chars
    if isinstance(exc, openai.APITimeoutError):
        phase = timeout_phase(exc)
        seconds = settings.phase_timeout(phase) if phase else settings.timeout_seconds
        return timeout_error(PROVIDER, seconds, phase=phase)
    if isinstance(exc, openai.APIConnectionError):
        if is_interrupted(exc):
            return interrupted_error(PROVIDER, exc)
        return connection_error(PROVIDER, exc)
    if isinstance(exc, openai.APIStatusError):
        response = getattr(exc, "response", None)
        return status_error(
            PROVIDER,
            exc.status_code,
            _error_detail(exc),
            limit,
            headers=getattr(response, "headers", None),
        )
    if isinstance(exc, openai.APIError):
        return ModelProviderError(
            f"{PROVIDER}: {preview(exc.message, limit) or 'unreadable response'}",
            provider=PROVIDER,
            kind="malformed_response",
        )
    return None


def _error_detail(exc: Any) -> str:
    """The provider's own error text: ``error.message`` of a JSON body, else the body."""

    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        message = body.get("message")
        if message:
            return str(message)
        return json.dumps(body, ensure_ascii=False)
    if body:
        return str(body)
    response = getattr(exc, "response", None)
    return str(getattr(response, "text", "") or "")


def _sdk_env_header_names() -> list[str]:
    """Header names the SDK would take from ``OPENAI_CUSTOM_HEADERS``, parsed its way."""

    raw = os.environ.get(_SDK_CUSTOM_HEADERS_ENV)
    if not raw:
        return []
    names = []
    for line in raw.split("\n"):
        name, colon, _ = line.partition(":")
        if colon and name.strip():
            names.append(name.strip())
    return names


def messages_to_wire(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Serialize the history, keeping tool calls and tool results strictly paired.

    Chat Completions rejects an assistant ``tool_calls`` entry without a ``tool`` reply
    before the next non-tool message, and a ``tool`` message answering no pending call.
    The history can hold both (a refused approval ends a task right after the call; a
    trimmed history can drop a call but keep its result), so unanswered calls are dropped
    from their assistant message and orphan results are skipped.
    """

    wire: list[dict[str, Any]] = []
    pending: set[str] = set()
    for index, message in enumerate(messages):
        if message.is_tool_call:
            answered = _answered_ids(messages, index + 1)
            calls = [call for call in message.tool_calls if call.id and call.id in answered]
            pending = {call.id for call in calls}
            if calls:
                wire.append(_tool_call_message(message.content, calls))
            elif message.content:
                wire.append({"role": "assistant", "content": message.content})
        elif message.role == "tool":
            if message.tool_call_id in pending:
                pending.discard(message.tool_call_id)
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
        else:
            pending = set()
            wire.append({"role": message.role, "content": message.content})
    return wire


def _answered_ids(messages: Sequence[Message], start: int) -> set[str]:
    """Ids answered by the run of ``tool`` messages starting at ``start``."""

    answered: set[str] = set()
    for message in messages[start:]:
        if message.role != "tool":
            break
        if message.tool_call_id:
            answered.add(message.tool_call_id)
    return answered


def _tool_call_message(content: str, calls: Sequence[ToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        # Some servers reject ``""`` next to tool calls.
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                },
            }
            for call in calls
        ],
    }


def _response_from_reply(reply: Any) -> ModelResponse:
    """Parse a ``ChatCompletion`` into a :class:`ModelResponse`."""

    choices = getattr(reply, "choices", None) or []
    if not choices:
        raise empty_response_error(PROVIDER)
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise empty_response_error(PROVIDER)
    tool_calls: list[ToolCall] = []
    seen: set[str] = set()
    for raw_call in getattr(message, "tool_calls", None) or []:
        if getattr(raw_call, "type", "function") != "function":
            continue
        function = getattr(raw_call, "function", None)
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        call_id = str(getattr(raw_call, "id", "") or "")
        if not call_id or call_id in seen:
            call_id = new_call_id()
        seen.add(call_id)
        tool_calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments_to_dict(getattr(function, "arguments", None)),
            )
        )
    return ModelResponse(
        content=_content_text(getattr(message, "content", None)),
        tool_calls=tuple(tool_calls),
        finish_reason=getattr(choice, "finish_reason", None),
        usage=_usage_from_reply(getattr(reply, "usage", None)),
    )


def _content_text(content: Any) -> str:
    """Message text; a list of content parts (some servers) is joined by its text parts."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = (
            part.get("text") if isinstance(part, Mapping) else getattr(part, "text", None)
            for part in content
        )
        return "".join(text for text in texts if isinstance(text, str))
    return str(content)


def _usage_from_reply(usage: Any) -> ModelUsage | None:
    if usage is None:
        return None
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return ModelUsage(
        input_tokens=optional_int(getattr(usage, "prompt_tokens", None)),
        output_tokens=optional_int(getattr(usage, "completion_tokens", None)),
        cached_input_tokens=optional_int(getattr(prompt_details, "cached_tokens", None)),
        reasoning_tokens=optional_int(getattr(completion_details, "reasoning_tokens", None)),
    )


__all__ = [
    "OpenAICompatChatModel",
    "build_client",
    "messages_to_wire",
    "openai_compat_llm_factory",
    "provider_error",
    "request_headers",
]
