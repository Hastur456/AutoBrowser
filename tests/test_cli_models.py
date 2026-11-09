"""The CLI builds the cascade tiers over the provider registry (``src/cli/models.py``)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.cli import models
from src.config import CascadeSettings, LLMSettings, ModelTierSpec, Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


LLM = LLMSettings(model="cheap", temperature=0.3, host="http://ollama:11434", timeout_seconds=20.0)


def test_a_tier_inherits_every_field_it_does_not_name() -> None:
    settings = models.tier_llm_settings(ModelTierSpec(model="strong"), LLM)

    assert settings.model == "strong"
    assert (settings.temperature, settings.host, settings.timeout_seconds) == (0.3, "http://ollama:11434", 20.0)
    assert settings.provider == "ollama"


def test_a_tier_overrides_what_it_names_and_null_sends_no_temperature() -> None:
    spec = ModelTierSpec(
        model="gpt", provider="openai_compat", host="https://api.example/v1", temperature=None, api_key="sk-x"
    )

    settings = models.tier_llm_settings(spec, LLM)

    assert (settings.provider, settings.host, settings.temperature) == (
        "openai_compat",
        "https://api.example/v1",
        None,
    )
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "sk-x"


def test_the_session_provider_applies_to_a_tier_without_its_own() -> None:
    settings = models.tier_llm_settings(
        ModelTierSpec(model="strong", host="https://x/v1"), LLM, provider="openai_compat"
    )

    assert settings.provider == "openai_compat"


def test_an_invalid_tier_fails_without_quoting_the_configuration() -> None:
    llm = LLM.model_copy(update={"extra_headers": {"X-Key": "header-secret"}})
    spec = ModelTierSpec(model="strong", provider="openai_compat", api_key="sk-secret")

    with pytest.raises(ValueError) as caught:
        models.tier_llm_settings(spec, llm.model_copy(update={"host": None}))

    message = str(caught.value)
    assert "strong" in message and "host" in message
    assert "sk-secret" not in message and "header-secret" not in message


def test_build_tier_hands_the_merged_settings_to_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_build(model: Any = None, temperature: Any = None, **kwargs: Any) -> str:
        seen.append({"model": model, "temperature": temperature, **kwargs})
        return "chat-model"

    monkeypatch.setattr(models, "build_chat_model", fake_build)
    monkeypatch.setenv("AUTOBROWSER_LLM__PRICES", '{"strong": {"input_per_mtok": 1, "output_per_mtok": 2}}')
    get_settings.cache_clear()

    tier = models.build_tier(ModelTierSpec(model="strong", label="big"), LLM)

    assert (tier.model, tier.label) == ("chat-model", "big")
    assert tier.price is not None and tier.price.output_per_mtok == 2.0
    assert seen[0]["model"] == "strong"
    assert seen[0]["settings"].model == "strong"


def test_no_factory_without_an_enabled_cascade_with_tiers() -> None:
    spec = [ModelTierSpec(model="strong")]

    assert models.cascade_factory(Settings(_env_file=None)) is None
    assert models.cascade_factory(Settings(_env_file=None, cascade=CascadeSettings(enabled=True))) is None
    assert models.cascade_factory(Settings(_env_file=None, cascade=CascadeSettings(tiers=spec))) is None


def test_the_factory_builds_every_tier_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []
    monkeypatch.setattr(models, "build_chat_model", lambda model=None, *_a, **_k: built.append(model) or model)
    settings = Settings(
        _env_file=None,
        cascade=CascadeSettings(enabled=True, tiers=[ModelTierSpec(model="a"), ModelTierSpec(model="b")]),
    )

    factory = models.cascade_factory(settings)
    assert factory is not None
    first, second = factory(), factory()

    assert [tier.label for tier in first] == ["a", "b"]
    assert built == ["a", "b", "a", "b"]
    assert first[0] is not second[0]


def test_check_tiers_reports_each_broken_tier_without_values() -> None:
    settings = Settings(
        _env_file=None,
        cascade=CascadeSettings(
            enabled=True,
            tiers=[
                ModelTierSpec(model="fine"),
                ModelTierSpec(model="broken", provider="openai_compat", api_key="sk-secret"),
            ],
        ),
    )

    problems = models.tier_problems(settings)

    assert len(problems) == 1
    assert "broken" in problems[0] and "sk-secret" not in problems[0]


def test_check_tiers_reports_a_tier_labelled_like_the_session_model() -> None:
    settings = Settings(
        _env_file=None,
        llm=LLMSettings(model="cheap"),
        cascade=CascadeSettings(enabled=True, tiers=[ModelTierSpec(model="cheap")]),
    )

    problems = models.tier_problems(settings)

    assert len(problems) == 1 and "label" in problems[0]
