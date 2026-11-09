"""The model cascade's tiers, built over the provider registry (``cascade.*``).

The only place that turns a :class:`~src.config.ModelTierSpec` into a
:class:`~src.llm.ChatModel`: the session receives the finished tiers through an injected
:data:`~src.harness.session.CascadeFactory` and the engine through ``EngineResources.models``,
so neither of them learns which provider a tier speaks. Tier 0 is always the session model;
these are the tiers above it.
"""

from __future__ import annotations

from pydantic import ValidationError

from src.config import LLMProvider, LLMSettings, ModelTierSpec, Settings
from src.harness.session import CascadeFactory, model_price
from src.llm import ModelTier
from src.providers.registry import build_chat_model


def tier_llm_settings(
    spec: ModelTierSpec, llm: LLMSettings, *, provider: LLMProvider | None = None
) -> LLMSettings:
    """``llm`` with the tier's overrides, revalidated (an ``openai_compat`` tier without a
    host fails here). A tier without a ``provider`` speaks the session's: ``provider``
    (``--provider``) or ``llm.provider``.

    A failure names the tier and the reasons, never a value: the merged settings hold keys
    and headers.
    """

    values = llm.model_dump()
    if provider is not None:
        values["provider"] = provider
    values.update(spec.llm_overrides())
    try:
        return LLMSettings.model_validate(values)
    except ValidationError as exc:
        reasons = "; ".join(
            str(error.get("msg", "")) for error in exc.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"cascade tier {spec.tier_label!r}: {reasons}") from None


def build_tier(
    spec: ModelTierSpec, llm: LLMSettings, *, provider: LLMProvider | None = None
) -> ModelTier:
    """One labelled, priced tier; its price is ``llm.prices[spec.model]``."""

    settings = tier_llm_settings(spec, llm, provider=provider)
    model = build_chat_model(settings.model, settings.temperature, settings=settings)
    return ModelTier(model, label=spec.tier_label, price=model_price(spec.model))


def cascade_factory(
    settings: Settings, *, provider: LLMProvider | None = None
) -> CascadeFactory | None:
    """The session's cascade factory; ``None`` unless ``cascade.enabled`` names a tier."""

    cascade = settings.cascade
    if not cascade.enabled or not cascade.tiers:
        return None
    llm = settings.llm
    specs = tuple(cascade.tiers)

    def build() -> list[ModelTier]:
        return [build_tier(spec, llm, provider=provider) for spec in specs]

    return build


def tier_problems(settings: Settings, *, provider: LLMProvider | None = None) -> list[str]:
    """Each cascade tier whose merged settings are invalid, checked offline (``--check``)."""

    cascade = settings.cascade
    if not cascade.enabled:
        return []
    problems: list[str] = []
    for spec in cascade.tiers:
        try:
            tier_llm_settings(spec, settings.llm, provider=provider)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


__all__ = ["build_tier", "cascade_factory", "tier_llm_settings", "tier_problems"]
