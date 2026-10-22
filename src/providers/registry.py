"""Provider registry: pick the chat model adapter by ``settings.llm.provider``.

:func:`build_chat_model` is the default ``llm_factory`` of the CLI composition
(``src/cli/bootstrap.py``); its ``model``/``temperature`` keywords match every existing
``llm_factory(...)`` call, so the harness and the engine never learn which protocol is in
use. A new protocol adds a value to ``LLMProvider``, a module here and a branch below.

:func:`probe_chat_provider` is the network check behind ``autobrowser doctor --llm``: it asks
the provider for its model list with the configured URL, key and headers, without retries.
Only ``src/cli/`` imports this package.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.config import LLMProvider, LLMSettings, get_settings
from src.llm import ChatModel, ModelProviderError
from src.providers import ollama, openai_compat


@dataclass(frozen=True)
class ProviderProbe:
    """Outcome of one ``doctor --llm`` probe.

    ``model_listed``/``models_count`` are ``None`` when the provider returned no list;
    ``error`` is a short text without secrets when the provider could not be reached.
    """

    reachable: bool
    model_listed: bool | None = None
    models_count: int | None = None
    error: str | None = None


def build_chat_model(
    model: str | None = None,
    temperature: float | None = None,
    *,
    provider: LLMProvider | None = None,
    settings: LLMSettings | None = None,
    **kwargs: Any,
) -> ChatModel:
    """Build the chat model of ``provider`` (``None``: ``settings.llm.provider``).

    Omitted ``model``/``temperature`` fall back to ``settings.llm``, as with every
    ``llm_factory``.
    """

    resolved = _settings_for(provider, settings)
    if resolved.provider == "ollama":
        return ollama.OllamaChatModel(model, temperature, settings=resolved, **kwargs)
    if resolved.provider == "openai_compat":
        return openai_compat.OpenAICompatChatModel(
            model, temperature, settings=resolved, **kwargs
        )
    raise ValueError(f"Unknown chat provider: {resolved.provider!r}")


def probe_chat_provider(settings: LLMSettings, *, timeout: float) -> ProviderProbe:
    """List the provider's models once, synchronously (``doctor`` is not async)."""

    return asyncio.run(_probe(settings, timeout))


async def _probe(settings: LLMSettings, timeout: float) -> ProviderProbe:
    try:
        if settings.provider == "openai_compat":
            names = await _openai_compat_models(settings, timeout)
        else:
            names = await _ollama_models(settings, timeout)
    except ModelProviderError as exc:
        return ProviderProbe(reachable=False, error=str(exc))
    except ValueError as exc:  # a configuration the client refuses (no host)
        return ProviderProbe(reachable=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        # The type only: an unexpected exception's text may quote a header.
        return ProviderProbe(reachable=False, error=f"unexpected {type(exc).__name__}")
    if names is None:
        return ProviderProbe(reachable=True)
    return ProviderProbe(
        reachable=True,
        model_listed=_listed(settings.model, names),
        models_count=len(names),
    )


async def _openai_compat_models(settings: LLMSettings, timeout: float) -> list[str] | None:
    client = openai_compat.build_client(settings, timeout=timeout, max_retries=0)
    try:
        page = await client.models.list(extra_headers=openai_compat.request_headers(settings))
    except Exception as exc:
        probe_settings = settings.model_copy(update={"timeout_seconds": timeout})
        error = openai_compat.provider_error(exc, probe_settings)
        if error is None:
            raise
        raise error from exc
    finally:
        await client.close()
    data = getattr(page, "data", None)
    if not isinstance(data, list):
        return None
    return [str(getattr(entry, "id", "") or "") for entry in data]


async def _ollama_models(settings: LLMSettings, timeout: float) -> list[str] | None:
    from ollama import AsyncClient

    client = AsyncClient(
        host=settings.host, timeout=timeout, headers=ollama.auth_headers(settings.api_key)
    )
    try:
        listing = await client.list()
    except Exception as exc:
        error = ollama.provider_error(exc, timeout, settings.error_preview_chars)
        if error is None:
            raise
        raise error from exc
    finally:
        await client._client.aclose()  # the ollama client has no close() of its own
    models = getattr(listing, "models", None)
    if not isinstance(models, list):
        return None
    return [str(getattr(entry, "model", "") or "") for entry in models]


def _listed(model: str, names: list[str]) -> bool:
    """Is ``model`` in ``names``? An untagged Ollama name also matches ``<name>:latest``."""

    return model in names or (":" not in model and f"{model}:latest" in names)


def _settings_for(provider: LLMProvider | None, settings: LLMSettings | None) -> LLMSettings:
    resolved = settings if settings is not None else get_settings().llm
    if provider is None or provider == resolved.provider:
        return resolved
    # Revalidated, so ``--provider openai_compat`` without ``llm.host`` fails here.
    return LLMSettings.model_validate({**resolved.model_dump(), "provider": provider})


__all__ = ["ProviderProbe", "build_chat_model", "probe_chat_provider"]
