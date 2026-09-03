"""Native Ollama chat provider implementing the provider-neutral ``ChatModel``.

This adapter talks to a local Ollama server over ``/api/chat`` through the
``ollama`` Python client (already a dependency) and exposes the
:class:`~src.llm.ChatModel.complete` contract the engine drives. The ``ollama``
package is imported lazily so importing this module never requires Ollama tooling
unless a model is actually constructed.

Only wire concerns live here: neutral ``Message``/``ToolDef`` objects are serialized to
Ollama's request shape and the reply is parsed back into a :class:`~src.llm.ModelResponse`.
Host resolution delegates to the ``ollama`` client (which honours ``OLLAMA_HOST``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from src.contracts import ToolDef
from src.llm import DEFAULT_OLLAMA_MODEL, ModelResponse
from src.messages import Message, ToolCall


class OllamaChatModel:
    """One Ollama model behind the provider-neutral :class:`ChatModel` contract."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        temperature: float = 0.0,
        base_url: str | None = None,
        timeout: float | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._temperature = float(temperature)
        if client is None:
            from ollama import AsyncClient  # lazy: only needed when a model is built

            client = AsyncClient(host=base_url, timeout=timeout)
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
            "options": _options(self._temperature, params),
        }
        if tools:
            request["tools"] = [_tool_to_wire(tool_def) for tool_def in tools]

        reply = await self._client.chat(**request)
        return _response_from_reply(reply)


def ollama_llm_factory(
    model: str = DEFAULT_OLLAMA_MODEL,
    temperature: float = 0.0,
    **kwargs: Any,
) -> OllamaChatModel:
    """Build an :class:`OllamaChatModel` (signature-compatible with ``llm_factory``)."""

    return OllamaChatModel(model=model, temperature=temperature, **kwargs)


def _options(temperature: float, params: Mapping[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": temperature}
    for key in ("num_predict", "num_ctx", "top_p", "seed"):
        if key in params:
            options[key] = params[key]
    return options


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


def _tool_to_wire(tool_def: ToolDef) -> dict[str, Any]:
    """Serialize one ``ToolDef`` into the OpenAI-compatible tool envelope Ollama accepts."""

    return {
        "type": "function",
        "function": {
            "name": tool_def.name,
            "description": tool_def.description,
            "parameters": tool_def.input_schema,
        },
    }


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
                id=f"call_{uuid4().hex}",
                name=name,
                arguments=_arguments_to_dict(getattr(function, "arguments", {})),
            )
        )
    return ModelResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=getattr(reply, "done_reason", None),
    )


def _arguments_to_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


__all__ = ["OllamaChatModel", "ollama_llm_factory"]
