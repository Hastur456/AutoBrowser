"""Tests for the Ollama adapter (:mod:`src.providers.ollama`) without a daemon.

A fake client records the ``chat`` request; replies are real ``ollama.ChatResponse``
objects so the parsing runs against the client's own types.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from ollama import ChatResponse, RequestError, ResponseError

from src.config import LLMSettings
from src.contracts import ToolDef
from src.llm import ModelProviderError, ModelUsage
from src.messages import (
    ToolCall,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from src.providers.ollama import OllamaChatModel


class FakeClient:
    def __init__(self, reply: Any = None, error: BaseException | None = None) -> None:
        self.reply = reply if reply is not None else ChatResponse(
            message={"role": "assistant", "content": "ok"}
        )
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def chat(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


def model_with(
    client: FakeClient, **settings: Any
) -> OllamaChatModel:
    return OllamaChatModel(settings=LLMSettings(**settings), client=client)


TOOL = ToolDef(name="lookup", description="Look something up.", input_schema={"type": "object"})


@pytest.mark.asyncio
async def test_request_carries_messages_tools_and_temperature() -> None:
    client = FakeClient()
    model = model_with(client, model="m1", temperature=0.3)

    await model.complete(
        [
            system_message("rules"),
            user_message("hi"),
            assistant_message(tool_calls=(ToolCall(id="c1", name="lookup", arguments={"q": "x"}),)),
            tool_message("c1", "found", name="lookup"),
        ],
        tools=[TOOL],
    )

    request = client.requests[0]
    assert request["model"] == "m1"
    assert request["options"] == {"temperature": 0.3}
    assert request["tools"][0]["function"]["name"] == "lookup"
    assert request["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"q": "x"}
    assert request["messages"][3] == {
        "role": "tool",
        "content": "found",
        "name": "lookup",
        "tool_call_id": "c1",
    }


@pytest.mark.asyncio
async def test_temperature_none_is_not_sent() -> None:
    client = FakeClient()

    await model_with(client, temperature=None).complete([user_message("hi")])

    assert "temperature" not in client.requests[0]["options"]


@pytest.mark.asyncio
async def test_max_output_tokens_becomes_num_predict_and_call_params_win() -> None:
    client = FakeClient()
    model = model_with(client, max_output_tokens=256)

    await model.complete([user_message("hi")])
    await model.complete([user_message("hi")], num_predict=8, unknown=1)

    assert client.requests[0]["options"]["num_predict"] == 256
    assert client.requests[1]["options"]["num_predict"] == 8
    assert "unknown" not in client.requests[1]["options"]


def test_the_configured_timeout_reaches_the_http_client() -> None:
    model = OllamaChatModel(settings=LLMSettings(timeout_seconds=12.5))

    assert model._client._client.timeout.read == 12.5


def test_a_null_timeout_waits_forever() -> None:
    model = OllamaChatModel(settings=LLMSettings(timeout_seconds=None))

    assert model._client._client.timeout.read is None


@pytest.mark.asyncio
async def test_reply_parses_tool_calls_and_usage() -> None:
    reply = ChatResponse(
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "x"}}}],
        },
        done_reason="stop",
        prompt_eval_count=120,
        eval_count=7,
        total_duration=2_500_000_000,
        load_duration=4_000_000,
    )

    response = await model_with(FakeClient(reply)).complete([user_message("hi")])

    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.tool_calls[0].id.startswith("call_")
    assert response.finish_reason == "stop"
    assert response.usage == ModelUsage(
        input_tokens=120, output_tokens=7, provider_duration_ms=2500, load_duration_ms=4
    )


@pytest.mark.asyncio
async def test_a_reply_without_counters_has_no_usage() -> None:
    response = await model_with(FakeClient()).complete([user_message("hi")])

    assert response.content == "ok"
    assert response.usage is None


@pytest.mark.parametrize(
    "error, status, retryable, text",
    [
        (ResponseError('{"error": "model not found"}', 404), 404, False, "ollama 404: model or endpoint not found: model not found"),
        (ResponseError("unauthorized secret-key", 401), 401, False, "ollama 401: authentication failed"),
        (ResponseError("overloaded", 503), 503, True, "ollama 503: overloaded"),
        (ConnectionError("refused"), None, True, "ollama: connection failed (ConnectionError)"),
        (httpx.ReadTimeout("slow"), None, True, "ollama: timed out after 60 s"),
        (RequestError("must provide a model"), None, False, "ollama: must provide a model"),
    ],
)
@pytest.mark.asyncio
async def test_client_errors_become_provider_errors(
    error: BaseException, status: int | None, retryable: bool, text: str
) -> None:
    model = model_with(FakeClient(error=error))

    with pytest.raises(ModelProviderError) as caught:
        await model.complete([user_message("hi")])

    assert str(caught.value) == text
    assert caught.value.provider == "ollama"
    assert caught.value.status_code == status
    assert caught.value.retryable is retryable
    assert "secret-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_error_bodies_are_truncated() -> None:
    model = model_with(FakeClient(error=ResponseError("x" * 500, 400)), error_preview_chars=40)

    with pytest.raises(ModelProviderError) as caught:
        await model.complete([user_message("hi")])

    assert len(str(caught.value)) <= len("ollama 400: ") + 40


@pytest.mark.asyncio
async def test_unrelated_exceptions_propagate_unchanged() -> None:
    model = model_with(FakeClient(error=KeyError("bug")))

    with pytest.raises(KeyError):
        await model.complete([user_message("hi")])
