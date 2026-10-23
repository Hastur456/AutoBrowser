"""Tests for the provider registry (:mod:`src.providers.registry`), no network.

``build_chat_model`` picks the adapter by ``llm.provider`` or an explicit ``provider=``;
``probe_chat_provider`` (``doctor --llm``) lists the provider's models once, over a mock
transport for ``openai_compat`` and a fake client for Ollama.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import ollama
import pytest

from src.config import LLMSettings
from src.llm import ModelProviderError
from src.providers import openai_compat, registry
from src.providers.ollama import OllamaChatModel
from src.providers.openai_compat import OpenAICompatChatModel
from src.providers.registry import ProviderProbe, build_chat_model, probe_chat_provider

HOST = "http://llm.test/v1"


def compat_settings(**overrides: Any) -> LLMSettings:
    return LLMSettings(**{"provider": "openai_compat", "host": HOST, **overrides})


# --------------------------------------------------------------------------
# build_chat_model
# --------------------------------------------------------------------------


def test_the_setting_picks_the_adapter() -> None:
    assert isinstance(build_chat_model(settings=LLMSettings()), OllamaChatModel)
    assert isinstance(build_chat_model(settings=compat_settings()), OpenAICompatChatModel)


def test_an_explicit_provider_overrides_the_setting() -> None:
    model = build_chat_model(provider="openai_compat", settings=LLMSettings(host=HOST))

    assert isinstance(model, OpenAICompatChatModel)
    assert model._settings.provider == "openai_compat"


def test_overriding_to_openai_compat_without_a_host_fails_loudly() -> None:
    with pytest.raises(ValueError, match="llm.host is required"):
        build_chat_model(provider="openai_compat", settings=LLMSettings())


def test_model_and_temperature_keywords_match_every_llm_factory_call() -> None:
    model = build_chat_model(model="other", temperature=0.4, settings=compat_settings())

    assert (model._model, model._temperature) == ("other", 0.4)


def test_omitted_model_and_temperature_come_from_the_settings() -> None:
    model = build_chat_model(settings=compat_settings(model="gemma-4", temperature=None))

    assert (model._model, model._temperature) == ("gemma-4", None)


def test_the_openai_sdk_is_not_loaded_for_ollama() -> None:
    code = (
        "import sys\n"
        "from src.config import LLMSettings\n"
        "from src.providers.registry import build_chat_model\n"
        "build_chat_model(settings=LLMSettings())\n"
        "print('openai' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert completed.stdout.strip() == "False"


# --------------------------------------------------------------------------
# probe_chat_provider: openai_compat over a mock transport
# --------------------------------------------------------------------------


def models_page(*ids: str) -> dict[str, Any]:
    data = [{"id": id_, "object": "model", "created": 0, "owned_by": "x"} for id_ in ids]
    return {"object": "list", "data": data}


def mock_transport(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    real_build_client = openai_compat.build_client

    def build_client(settings: LLMSettings, **kwargs: Any) -> Any:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return real_build_client(settings, http_client=http_client, **kwargs)

    monkeypatch.setattr(openai_compat, "build_client", build_client)
    return requests


def test_a_listed_model_probes_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = mock_transport(monkeypatch, httpx.Response(200, json=models_page("gemma-4", "gpt-5")))

    probe = probe_chat_provider(compat_settings(model="gemma-4", api_key="sk-x"), timeout=5)

    assert probe == ProviderProbe(reachable=True, model_listed=True, models_count=2)
    assert str(requests[0].url) == f"{HOST}/models"
    assert requests[0].headers["authorization"] == "Bearer sk-x"


def test_an_unlisted_model_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_transport(monkeypatch, httpx.Response(200, json=models_page("gpt-5")))

    probe = probe_chat_provider(compat_settings(model="gemma-4"), timeout=5)

    assert probe == ProviderProbe(reachable=True, model_listed=False, models_count=1)


@pytest.mark.parametrize(
    "response, text",
    [
        (
            httpx.Response(401, json={"error": {"message": "bad key sk-secret"}}),
            "openai_compat 401: authentication failed",
        ),
        (
            httpx.Response(404, json={"error": {"message": "no route"}}),
            "openai_compat 404: model or endpoint not found: no route",
        ),
        (
            httpx.ConnectError("refused"),
            "openai_compat: connection failed (APIConnectionError <- ConnectError)",
        ),
        (httpx.ReadTimeout("slow"), "openai_compat: timed out after 5 s"),
    ],
)
def test_failures_are_unreachable_without_retries_or_secrets(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception, text: str
) -> None:
    requests = mock_transport(monkeypatch, response)

    probe = probe_chat_provider(compat_settings(api_key="sk-secret", max_retries=5), timeout=5)

    assert probe == ProviderProbe(reachable=False, error=text)
    assert len(requests) == 1
    assert "sk-secret" not in (probe.error or "")


def test_list_chat_models_returns_the_ids_in_provider_order(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_transport(monkeypatch, httpx.Response(200, json=models_page("gpt-5", "gemma-4")))

    names = asyncio.run(registry.list_chat_models(settings=compat_settings()))

    assert names == ["gpt-5", "gemma-4"]


def test_list_chat_models_raises_the_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_transport(monkeypatch, httpx.Response(401, json={"error": {"message": "bad key"}}))

    with pytest.raises(ModelProviderError, match="authentication failed"):
        asyncio.run(registry.list_chat_models(settings=compat_settings()))


def test_list_chat_models_reports_an_unexpected_failure_by_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("header Bearer sk-secret")

    monkeypatch.setattr(openai_compat, "build_client", broken)

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(registry.list_chat_models(settings=compat_settings()))

    assert str(caught.value) == "openai_compat: unexpected TypeError"


def test_list_chat_models_wraps_a_malformed_ollama_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ollama(monkeypatch, error=ValueError("1 validation error input_value={'secret': 1}"))

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(registry.list_chat_models(settings=LLMSettings()))

    assert str(caught.value) == "ollama: unexpected ValueError"


def test_list_chat_models_raises_the_missing_host_as_a_configuration_error() -> None:
    settings = LLMSettings().model_copy(update={"provider": "openai_compat"})

    with pytest.raises(ValueError, match="llm.host is required"):
        asyncio.run(registry.list_chat_models(settings=settings))


def test_list_chat_models_follows_a_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ollama.ListResponse(models=[{"model": "qwen3:8b"}])
    fake_ollama(monkeypatch, listing)

    names = asyncio.run(registry.list_chat_models(provider="ollama", settings=compat_settings()))

    assert names == ["qwen3:8b"]


def test_a_missing_host_is_reported_not_raised() -> None:
    settings = LLMSettings().model_copy(update={"provider": "openai_compat"})

    probe = probe_chat_provider(settings, timeout=5)

    assert not probe.reachable
    assert "llm.host is required" in (probe.error or "")


# --------------------------------------------------------------------------
# probe_chat_provider: ollama over a fake client
# --------------------------------------------------------------------------


class FakeOllamaClient:
    created: list[dict[str, Any]] = []

    def __init__(self, listing: Any = None, error: Exception | None = None, **kwargs: Any) -> None:
        self.listing = listing
        self.error = error
        self.closed = False
        FakeOllamaClient.created.append(kwargs)
        self._client = SimpleNamespace(aclose=self._aclose)

    async def _aclose(self) -> None:
        self.closed = True

    async def list(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.listing


def fake_ollama(
    monkeypatch: pytest.MonkeyPatch, listing: Any = None, error: Exception | None = None
) -> None:
    FakeOllamaClient.created = []
    monkeypatch.setattr(
        ollama, "AsyncClient", lambda **kwargs: FakeOllamaClient(listing, error, **kwargs)
    )


def test_ollama_lists_its_models(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ollama.ListResponse(models=[{"model": "gemma4:latest"}, {"model": "qwen3:8b"}])
    fake_ollama(monkeypatch, listing)

    probe = probe_chat_provider(LLMSettings(model="gemma4", api_key="k"), timeout=3)

    assert probe == ProviderProbe(reachable=True, model_listed=True, models_count=2)
    assert FakeOllamaClient.created[0] == {
        "host": None,
        "timeout": 3,
        "headers": {"Authorization": "Bearer k"},
    }


def test_ollama_connection_failure_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ollama(monkeypatch, error=ConnectionError("refused"))

    probe = probe_chat_provider(LLMSettings(), timeout=3)

    assert probe == ProviderProbe(
        reachable=False, error="ollama: connection failed (ConnectionError)"
    )


def test_an_unexpected_failure_is_reported_by_type_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("header Bearer sk-secret")

    monkeypatch.setattr(openai_compat, "build_client", broken)

    probe = probe_chat_provider(compat_settings(), timeout=5)

    assert probe == ProviderProbe(reachable=False, error="unexpected TypeError")


def test_registry_exports() -> None:
    assert set(registry.__all__) == {
        "ProviderProbe",
        "build_chat_model",
        "list_chat_models",
        "probe_chat_provider",
    }
