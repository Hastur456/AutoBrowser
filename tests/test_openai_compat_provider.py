"""Tests for the OpenAI-compatible adapter (:mod:`src.providers.openai_compat`), no network.

Two layers: a fake client records the ``chat.completions.create`` kwargs (serialization,
parsing, error mapping); a real ``AsyncOpenAI`` over ``httpx.MockTransport`` pins what
actually leaves the process (path, headers, the neutralized ``OPENAI_*`` environment,
retries) on the locked SDK version.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

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
from src.providers.openai_compat import OpenAICompatChatModel, build_client

HOST = "http://llm.test/v1"
TOOL = ToolDef(name="lookup", description="Look something up.", input_schema={"type": "object"})


def settings(**overrides: Any) -> LLMSettings:
    return LLMSettings(**{"provider": "openai_compat", "host": HOST, **overrides})


def completion(
    content: str | None = "ok",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    finish_reason: str = "stop",
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        **extra,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def function_call(call_id: str | None, name: str, arguments: str) -> dict[str, Any]:
    call: dict[str, Any] = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        call["id"] = call_id
    return call


class FakeCompletions:
    def __init__(self, reply: Any = None, error: BaseException | None = None) -> None:
        self.reply = reply
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


def fake_model(
    reply: dict[str, Any] | None = None,
    error: BaseException | None = None,
    **overrides: Any,
) -> tuple[OpenAICompatChatModel, FakeCompletions]:
    # ``construct`` builds nested models without validation, as the SDK does with a
    # reply (``_strict_response_validation`` off), so loose vendor replies parse too.
    parsed = openai.types.chat.ChatCompletion.construct(**reply) if reply else None
    completions = FakeCompletions(parsed, error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAICompatChatModel(settings=settings(**overrides), client=client), completions


# --------------------------------------------------------------------------
# Request serialization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_role_is_serialized_with_arguments_as_a_json_string() -> None:
    model, completions = fake_model(completion(), model="m1")

    await model.complete(
        [
            system_message("rules"),
            user_message("hi"),
            assistant_message(
                tool_calls=(ToolCall(id="call_abc", name="lookup", arguments={"q": "ё"}),)
            ),
            tool_message("call_abc", "found", name="lookup"),
            assistant_message("done"),
        ],
        tools=[TOOL],
    )

    request = completions.requests[0]
    assert request["model"] == "m1"
    assert request["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q": "ё"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc", "content": "found"},
        {"role": "assistant", "content": "done"},
    ]
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look something up.",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert "strict" not in request["tools"][0]["function"]


@pytest.mark.asyncio
async def test_unanswered_tool_calls_and_orphan_results_are_not_sent() -> None:
    """A refused approval leaves a call without a result; the wire must stay paired."""

    model, completions = fake_model(completion())

    await model.complete(
        [
            user_message("task 1"),
            assistant_message(tool_calls=(ToolCall(id="c1", name="buy", arguments={}),)),
            assistant_message("Blocked: approval refused."),
            tool_message("ghost", "orphan result"),
            user_message("task 2"),
            assistant_message(
                "thinking",
                tool_calls=(
                    ToolCall(id="c2", name="lookup", arguments={}),
                    ToolCall(id="c3", name="lookup", arguments={}),
                ),
            ),
            tool_message("c2", "answer"),
        ]
    )

    messages = completions.requests[0]["messages"]
    assert messages == [
        {"role": "user", "content": "task 1"},
        {"role": "assistant", "content": "Blocked: approval refused."},
        {"role": "user", "content": "task 2"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "answer"},
    ]


@pytest.mark.asyncio
async def test_optional_parameters_are_sent_only_when_set() -> None:
    model, completions = fake_model(completion())

    await model.complete([user_message("hi")])

    request = completions.requests[0]
    assert request["temperature"] == 0.0
    for key in (
        "max_completion_tokens",
        "max_tokens",
        "reasoning_effort",
        "parallel_tool_calls",
        "tools",
        "extra_body",
        "stream",
    ):
        assert key not in request


@pytest.mark.asyncio
async def test_temperature_none_is_not_sent() -> None:
    model, completions = fake_model(completion(), temperature=None)

    await model.complete([user_message("hi")])

    assert "temperature" not in completions.requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("param", ["max_completion_tokens", "max_tokens"])
async def test_the_output_cap_uses_the_configured_field_name(param: str) -> None:
    model, completions = fake_model(completion(), max_output_tokens=512, max_tokens_param=param)

    await model.complete([user_message("hi")])

    request = completions.requests[0]
    assert request[param] == 512
    other = {"max_completion_tokens", "max_tokens"} - {param}
    assert not other & request.keys()


@pytest.mark.asyncio
async def test_reasoning_effort_extra_body_and_call_params_are_forwarded() -> None:
    model, completions = fake_model(
        completion(), reasoning_effort="low", extra_body={"reasoning": {"max_tokens": 64}}
    )

    await model.complete([user_message("hi")], top_p=0.9, seed=7, num_ctx=4096)

    request = completions.requests[0]
    assert request["reasoning_effort"] == "low"
    assert request["extra_body"] == {"reasoning": {"max_tokens": 64}}
    assert (request["top_p"], request["seed"]) == (0.9, 7)
    assert "num_ctx" not in request


@pytest.mark.asyncio
async def test_parallel_tool_calls_only_accompanies_tools() -> None:
    model, completions = fake_model(completion(), parallel_tool_calls=False)

    await model.complete([user_message("hi")])
    await model.complete([user_message("hi")], tools=[TOOL])

    assert "parallel_tool_calls" not in completions.requests[0]
    assert completions.requests[1]["parallel_tool_calls"] is False


# --------------------------------------------------------------------------
# Reply parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_call_keeps_the_provider_id_for_the_round_trip() -> None:
    reply = completion(
        content=None,
        tool_calls=[function_call("call_abc", "lookup", '{"q": "x"}')],
        finish_reason="tool_calls",
    )
    model, _ = fake_model(reply)

    response = await model.complete([user_message("hi")], tools=[TOOL])

    assert response.content == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (ToolCall(id="call_abc", name="lookup", arguments={"q": "x"}),)


@pytest.mark.asyncio
async def test_missing_or_repeated_ids_are_replaced() -> None:
    reply = completion(
        tool_calls=[
            function_call(None, "a", "{}"),
            function_call("dup", "b", "{}"),
            function_call("dup", "c", "{}"),
        ]
    )
    model, _ = fake_model(reply)

    calls = (await model.complete([user_message("hi")])).tool_calls

    assert calls[0].id.startswith("call_")
    assert calls[1].id == "dup"
    assert calls[2].id.startswith("call_") and calls[2].id != "dup"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", ""])
async def test_invalid_arguments_become_an_empty_object(arguments: str) -> None:
    model, _ = fake_model(completion(tool_calls=[function_call("c", "lookup", arguments)]))

    calls = (await model.complete([user_message("hi")])).tool_calls

    assert calls[0].arguments == {}


@pytest.mark.asyncio
async def test_non_function_and_nameless_calls_are_skipped() -> None:
    reply = completion(
        tool_calls=[
            {"id": "x", "type": "custom", "custom": {"name": "free", "input": "..."}},
            function_call("y", "", "{}"),
            function_call("z", "lookup", "{}"),
        ]
    )
    model, _ = fake_model(reply)

    calls = (await model.complete([user_message("hi")])).tool_calls

    assert [call.id for call in calls] == ["z"]


@pytest.mark.asyncio
async def test_vendor_fields_and_reasoning_are_ignored() -> None:
    reply = completion(content="answer", lama={"cost": 0.1, "currency": "RUB"})
    reply["choices"][0]["message"]["reasoning_content"] = "secret thoughts"
    model, _ = fake_model(reply)

    response = await model.complete([user_message("hi")])

    assert response.content == "answer"


@pytest.mark.asyncio
async def test_usage_is_parsed_and_missing_fields_stay_none() -> None:
    reply = completion(
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 64},
            "completion_tokens_details": {"reasoning_tokens": 12},
        }
    )
    model, _ = fake_model(reply)

    usage = (await model.complete([user_message("hi")])).usage

    assert usage == ModelUsage(
        input_tokens=100, output_tokens=20, cached_input_tokens=64, reasoning_tokens=12
    )

    model, _ = fake_model(completion(usage={"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}))
    usage = (await model.complete([user_message("hi")])).usage
    assert usage == ModelUsage(input_tokens=5, output_tokens=0)

    model, _ = fake_model(completion())
    assert (await model.complete([user_message("hi")])).usage is None


@pytest.mark.asyncio
async def test_empty_choices_is_a_provider_error() -> None:
    reply = completion()
    reply["choices"] = []
    model, _ = fake_model(reply)

    with pytest.raises(ModelProviderError, match="openai_compat: empty response"):
        await model.complete([user_message("hi")])


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------


def status_error(status: int, body: Any) -> openai.APIStatusError:
    request = httpx.Request("POST", f"{HOST}/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    error_cls = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        404: openai.NotFoundError,
        429: openai.RateLimitError,
    }.get(status, openai.APIStatusError)
    return error_cls("failed", response=response, body=body.get("error", body))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, text, status, retryable",
    [
        (
            status_error(400, {"error": {"message": "Unsupported parameter: 'temperature'"}}),
            "openai_compat 400: Unsupported parameter: 'temperature'",
            400,
            False,
        ),
        (
            status_error(401, {"error": {"message": "Incorrect API key sk-secret"}}),
            "openai_compat 401: authentication failed",
            401,
            False,
        ),
        (
            status_error(402, {"error": {"message": "balance is 0"}}),
            "openai_compat 402: payment required / insufficient balance: balance is 0",
            402,
            False,
        ),
        (
            status_error(404, {"error": {"message": "no such model"}}),
            "openai_compat 404: model or endpoint not found: no such model",
            404,
            False,
        ),
        (
            status_error(429, {"error": {"message": "slow down"}}),
            "openai_compat 429: slow down",
            429,
            True,
        ),
        (
            status_error(503, {"detail": "overloaded"}),
            'openai_compat 503: {"detail": "overloaded"}',
            503,
            True,
        ),
        (
            openai.APIConnectionError(request=httpx.Request("POST", HOST)),
            "openai_compat: connection failed (APIConnectionError)",
            None,
            True,
        ),
        (
            openai.APITimeoutError(request=httpx.Request("POST", HOST)),
            "openai_compat: timed out after 60 s",
            None,
            True,
        ),
    ],
)
async def test_sdk_errors_become_provider_errors(
    error: BaseException, text: str, status: int | None, retryable: bool
) -> None:
    model, _ = fake_model(error=error)

    with pytest.raises(ModelProviderError) as caught:
        await model.complete([user_message("hi")])

    assert str(caught.value) == text
    assert (caught.value.provider, caught.value.status_code, caught.value.retryable) == (
        "openai_compat",
        status,
        retryable,
    )
    assert "sk-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_error_text_is_truncated() -> None:
    model, _ = fake_model(
        error=status_error(400, {"error": {"message": "x" * 500}}), error_preview_chars=40
    )

    with pytest.raises(ModelProviderError) as caught:
        await model.complete([user_message("hi")])

    assert len(str(caught.value)) <= len("openai_compat 400: ") + 40


@pytest.mark.asyncio
async def test_unrelated_exceptions_propagate_unchanged() -> None:
    model, _ = fake_model(error=KeyError("bug"))

    with pytest.raises(KeyError):
        await model.complete([user_message("hi")])


# --------------------------------------------------------------------------
# What leaves the process: a real AsyncOpenAI over a mock transport
# --------------------------------------------------------------------------


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def transport_model(
    recorder: Recorder, **overrides: Any
) -> OpenAICompatChatModel:
    config = settings(**overrides)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    client = build_client(config, http_client=http_client)
    return OpenAICompatChatModel(settings=config, client=client)


def ok() -> httpx.Response:
    return httpx.Response(200, json=completion())


@pytest.mark.asyncio
async def test_the_request_goes_to_the_configured_chat_completions_path() -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, model="gemma-4").complete([user_message("hi")])

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{HOST}/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "gemma-4"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "stream" not in body


@pytest.mark.asyncio
async def test_a_configured_key_is_sent_as_bearer() -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, api_key="sk-test").complete([user_message("hi")])

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", [None, "", "   "])
async def test_without_a_key_no_authorization_header_is_sent(api_key: str | None) -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, api_key=api_key).complete([user_message("hi")])

    assert "authorization" not in recorder.requests[0].headers


@pytest.mark.asyncio
async def test_the_openai_environment_is_neutralized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.test/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-env")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-env")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Env-Header: leaked\nX-Title: env")
    recorder = Recorder(ok())

    model = transport_model(recorder, extra_headers={"X-Title": "AutoBrowser"})
    await model.complete([user_message("hi")])

    request = recorder.requests[0]
    assert request.url.host == "llm.test"
    headers = {name.lower(): value for name, value in request.headers.items()}
    assert "authorization" not in headers
    assert "openai-organization" not in headers
    assert "openai-project" not in headers
    assert "x-env-header" not in headers
    assert headers["x-title"] == "AutoBrowser"
    assert not any("env" in value for value in headers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Authorization", "authorization"])
async def test_an_authorization_from_extra_headers_is_sent_without_a_key(name: str) -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, extra_headers={name: "Basic abc"}).complete(
        [user_message("hi")]
    )

    assert recorder.requests[0].headers.get_list("authorization") == ["Basic abc"]


@pytest.mark.asyncio
async def test_an_empty_authorization_in_extra_headers_does_not_blank_the_key() -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, api_key="k1", extra_headers={"authorization": " "}).complete(
        [user_message("hi")]
    )

    assert recorder.requests[0].headers.get_list("authorization") == ["Bearer k1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("env_name", ["Authorization", "authorization"])
async def test_an_environment_authorization_never_displaces_the_key(
    monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", f"{env_name}: Bearer sk-env")
    recorder = Recorder(ok(), ok())

    await transport_model(recorder, api_key="sk-test").complete([user_message("hi")])
    await transport_model(recorder).complete([user_message("hi")])

    with_key, without_key = recorder.requests
    assert with_key.headers.get_list("authorization") == ["Bearer sk-test"]
    assert "authorization" not in without_key.headers


@pytest.mark.asyncio
async def test_a_connection_failure_names_its_cause() -> None:
    recorder = Recorder(ok())
    config = settings()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = build_client(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    model = OpenAICompatChatModel(settings=config, client=client)

    with pytest.raises(ModelProviderError) as caught:
        await model.complete([user_message("hi")])

    assert str(caught.value) == "openai_compat: connection failed (APIConnectionError <- ConnectError)"
    assert not recorder.requests


@pytest.mark.asyncio
async def test_extra_headers_are_sent() -> None:
    recorder = Recorder(ok())

    await transport_model(recorder, extra_headers={"HTTP-Referer": "https://x.test"}).complete(
        [user_message("hi")]
    )

    assert recorder.requests[0].headers["http-referer"] == "https://x.test"


@pytest.mark.asyncio
async def test_no_retry_by_default() -> None:
    recorder = Recorder(httpx.Response(429, json={"error": {"message": "slow down"}}))

    with pytest.raises(ModelProviderError) as caught:
        await transport_model(recorder).complete([user_message("hi")])

    assert caught.value.status_code == 429
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_max_retries_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("anyio.sleep", no_sleep)
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    recorder = Recorder(
        httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after-ms": "1"}),
        ok(),
    )

    response = await transport_model(recorder, max_retries=1).complete([user_message("hi")])

    assert response.content == "ok"
    assert len(recorder.requests) == 2


def test_a_missing_host_never_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.test/v1")

    with pytest.raises(ValueError, match="llm.host is required"):
        build_client(LLMSettings(provider="ollama"))


def test_the_client_takes_timeout_and_retries_from_settings() -> None:
    client = build_client(settings(timeout_seconds=12.0, max_retries=3))

    assert client.timeout == 12.0
    assert client.max_retries == 3
