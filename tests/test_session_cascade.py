"""The session composes the model cascade: tier 0 is the session model, the CLI's factory
builds the tiers above it (``SessionContext.model_tiers``, ``build_agent``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.config import get_settings
from src.harness.session import SessionConfig, SessionRuntime, model_price
from src.llm import ModelPrice, ModelTier


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


class FakeLLM:
    def __init__(self, model: str = "") -> None:
        self.model = model


class FakeMCPRuntime:
    def __init__(self) -> None:
        self.browser_server: str | None = None

    async def start(self, *, require_browser: bool = True) -> None: ...

    async def context_sections(self, **_kwargs: Any) -> list[Any]:
        return []

    async def close(self) -> None: ...


def config(**overrides: Any) -> SessionConfig:
    values: dict[str, Any] = {
        "model": "cheap",
        "temperature": 0.1,
        "no_mcp": True,
        "show_state": False,
        "show_tools": False,
        "as_json": False,
        "compress_tools": False,
        "agent_loop": False,
        "chrome_path": "chrome.exe",
        "user_data_dir": "profile",
        "cdp_port": 9555,
        "cdp_timeout": 1.0,
        "turn_cap": 10,
    }
    values.update(overrides)
    return SessionConfig(**values)


async def noop_wait(_port: int, _timeout: float) -> None:
    return None


def runtime(cascade: Any = None, *, output: list[str] | None = None, **overrides: Any) -> SessionRuntime:
    return SessionRuntime(
        config(**overrides),
        llm_factory=lambda **kwargs: FakeLLM(str(kwargs.get("model") or "")),
        start_chrome_cdp=lambda *_args: None,
        wait_for_port=noop_wait,
        mcp_runtime_factory=lambda *_args: FakeMCPRuntime(),
        cascade_factory=cascade,
        output_fn=(output.append if output is not None else (lambda *_args: None)),
    )


def strong_tiers() -> list[ModelTier]:
    return [ModelTier(FakeLLM("strong"), "strong", ModelPrice(5.0, 5.0))]


def set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_without_a_cascade_the_agent_has_one_tier_the_session_model() -> None:
    session = runtime()
    await session.start()
    try:
        tiers = session.agent.resources.models
        assert tiers is not None
        assert [tier.label for tier in tiers.tiers] == ["cheap"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_the_cascade_tiers_follow_the_session_model() -> None:
    calls: list[int] = []

    def cascade() -> list[ModelTier]:
        calls.append(1)
        return strong_tiers()

    session = runtime(cascade)
    await session.start()
    try:
        tiers = session.agent.resources.models
        assert [tier.label for tier in tiers.tiers] == ["cheap", "strong"]
        assert tiers.tiers[0].model is session.context.llm
        assert calls == [1]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_switching_the_model_changes_only_tier_0() -> None:
    session = runtime(strong_tiers)
    await session.start()
    try:
        await session.set_model("other")
        assert [tier.label for tier in session.agent.resources.models.tiers] == ["other", "strong"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_switching_to_a_tier_label_is_refused_and_changes_nothing() -> None:
    session = runtime(strong_tiers)
    await session.start()
    try:
        agent, llm = session.agent, session.context.llm

        with pytest.raises(ValueError, match="cascade tier"):
            await session.set_model("strong")

        assert session.config.model == "cheap" and session.context.config.model == "cheap"
        assert session.agent is agent and session.context.llm is llm
        assert session.context.harness.llm is llm
        assert [tier.label for tier in session.agent.resources.models.tiers] == ["cheap", "strong"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_a_session_not_started_refuses_a_tier_label_too(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(
        monkeypatch,
        AUTOBROWSER_CASCADE__ENABLED="true",
        AUTOBROWSER_CASCADE__TIERS='[{"model": "big", "label": "strong"}]',
    )
    session = runtime(strong_tiers)

    with pytest.raises(ValueError, match="cascade tier"):
        await session.set_model("strong")

    assert session.config.model == "cheap"


@pytest.mark.asyncio
async def test_a_tier_labelled_like_the_session_model_is_refused() -> None:
    session = runtime(lambda: [ModelTier(FakeLLM(), "cheap")])

    with pytest.raises(ValueError, match="give it a label"):
        await session.start()
    await session.close()


@pytest.mark.asyncio
async def test_the_retarget_tier_drives_the_retargeter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_env(
        monkeypatch,
        AUTOBROWSER_REPLAY__ENABLED="true",
        AUTOBROWSER_CASCADE__ENABLED="true",
        AUTOBROWSER_CASCADE__TIERS='[{"model": "strong"}]',
        AUTOBROWSER_CASCADE__RETARGET_TIER="1",
    )
    session = runtime(strong_tiers)
    await session.start()
    try:
        tiers = session.agent.resources.models
        assert tiers.retarget_tier == 1
        retargeter = session.context.playback_factory._retargeter  # noqa: SLF001
        assert retargeter is not None
        assert (retargeter._label, retargeter._tier) == ("strong", 1)  # noqa: SLF001
        assert retargeter._llm is tiers.tiers[1].model  # noqa: SLF001
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_a_money_cap_without_a_price_for_every_tier_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(
        monkeypatch,
        AUTOBROWSER_LOOP__MAX_TASK_COST="0.5",
        AUTOBROWSER_LLM__PRICES='{"cheap": {"input_per_mtok": 1, "output_per_mtok": 1}}',
    )
    output: list[str] = []
    session = runtime(lambda: [ModelTier(FakeLLM(), "strong", None)], output=output)
    await session.start()
    await session.close()

    assert any("no price" in line and "strong" in line for line in output)


@pytest.mark.asyncio
async def test_every_tier_priced_gives_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(
        monkeypatch,
        AUTOBROWSER_LOOP__MAX_TASK_COST="0.5",
        AUTOBROWSER_LLM__PRICES='{"cheap": {"input_per_mtok": 1, "output_per_mtok": 1}}',
    )
    output: list[str] = []
    session = runtime(strong_tiers, output=output)
    await session.start()
    await session.close()

    assert not any("no price" in line for line in output)


def test_model_price_reads_llm_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, AUTOBROWSER_LLM__PRICES='{"m": {"input_per_mtok": 2, "output_per_mtok": 3}}')

    assert model_price("m") == ModelPrice(2.0, 3.0)
    assert model_price("other") is None

