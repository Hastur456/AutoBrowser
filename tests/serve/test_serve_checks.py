"""What stops the stand from starting, before any browser runs."""

from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.serve.checks import startup_problems, startup_warnings, trigger_secret
from tests.serve.serve_fakes import job


def settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, **fields)


def test_a_plain_stand_has_no_problem() -> None:
    assert startup_problems(settings(serve={"jobs": {"j": job()}})) == []


def test_bypass_is_refused() -> None:
    problems = startup_problems(settings(permissions={"mode": "bypass"}))

    assert any("bypass" in problem for problem in problems)


@pytest.mark.parametrize(
    "fields",
    [
        {"probe": {"steps": [{"tool": "snapshot"}]}},
        {"recording": "price-watch"},
    ],
)
def test_probes_and_recordings_need_replay(fields: dict[str, Any]) -> None:
    problems = startup_problems(settings(serve={"jobs": {"j": job(**fields)}}))

    assert any("replay.enabled" in problem for problem in problems)
    assert startup_problems(
        settings(replay={"enabled": True}, serve={"jobs": {"j": job(**fields)}})
    ) == []


@pytest.mark.parametrize(
    "serve",
    [
        {"jobs": {"j": job(budget={"max_model_calls": 3, "max_cost": 0.1})}},
        {"default_run_budget": {"max_model_calls": 3, "max_cost": 0.1}},
        {"daily_max_cost": 1.0},
    ],
)
def test_a_money_cap_needs_a_price_for_the_model(serve: dict[str, Any]) -> None:
    unpriced = startup_problems(settings(llm={"model": "m1"}, serve=serve))
    priced = startup_problems(
        settings(llm={"model": "m1", "prices": {"m1": {"input_per_mtok": 1, "output_per_mtok": 2}}}, serve=serve)
    )

    assert any("llm.prices" in problem for problem in unpriced)
    assert priced == []


PRICED = {"input_per_mtok": 1, "output_per_mtok": 2}
CASCADE = {"enabled": True, "tiers": [{"model": "m2"}]}


def test_a_money_cap_needs_a_price_for_every_cascade_tier() -> None:
    money = {"daily_max_cost": 1.0}
    unpriced = startup_problems(
        settings(llm={"model": "m1", "prices": {"m1": PRICED}}, cascade=CASCADE, serve=money)
    )
    priced = startup_problems(
        settings(llm={"model": "m1", "prices": {"m1": PRICED, "m2": PRICED}}, cascade=CASCADE, serve=money)
    )
    disabled = startup_problems(
        settings(
            llm={"model": "m1", "prices": {"m1": PRICED}},
            cascade={**CASCADE, "enabled": False},
            serve=money,
        )
    )

    assert any("'m2'" in problem for problem in unpriced)
    assert priced == [] and disabled == []


def test_the_task_cost_cap_also_needs_the_prices() -> None:
    problems = startup_problems(settings(llm={"model": "m1"}, loop={"max_task_cost": 0.2}))

    assert any("llm.prices" in problem and "'m1'" in problem for problem in problems)


def test_a_webhook_secret_from_an_unset_variable_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = {"id": "ci", "type": "webhook", "secret_env": "PRICE_HOOK_SECRET"}
    monkeypatch.delenv("PRICE_HOOK_SECRET", raising=False)
    config = settings(serve={"jobs": {"j": job(triggers=[hook])}})

    assert any("PRICE_HOOK_SECRET" in problem for problem in startup_problems(config))

    monkeypatch.setenv("PRICE_HOOK_SECRET", "s3cret")
    assert startup_problems(config) == []
    assert trigger_secret(config.serve.jobs["j"].triggers[0]) == "s3cret"


def test_problems_never_quote_a_secret() -> None:
    config = settings(
        permissions={"mode": "bypass"},
        serve={"token": "t0ken-value", "jobs": {"j": job(triggers=[{"id": "ci", "type": "webhook", "secret": "s3cret"}])}},
    )

    text = " ".join(startup_problems(config))

    assert "t0ken-value" not in text and "s3cret" not in text


def test_the_docker_stand_profile_is_valid_and_startable(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from src.config import CONFIG_FILE_ENV_VAR

    profile = Path(__file__).resolve().parents[2] / "docker" / "serve.example.yaml"
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    monkeypatch.setenv("AUTOBROWSER_SERVE__TOKEN", "t0ken")

    loaded = Settings(_env_file=None)

    assert loaded.browser.launch == "external"
    assert loaded.serve.host == "0.0.0.0" and loaded.serve.allow_remote
    assert "price-watch" in loaded.serve.jobs
    assert startup_problems(loaded) == []


def test_a_pool_needs_persistent_memory_off() -> None:
    pool = {"workers": 2}

    problems = startup_problems(settings(memory={"persistent_enabled": True}, serve=pool))

    assert any("memory.persistent_enabled" in problem for problem in problems)
    assert startup_problems(settings(serve=pool)) == []
    assert startup_problems(settings(memory={"persistent_enabled": True})) == []


EXTERNAL_SHARED = {"playwright": {"transport": "stdio", "command": "npx", "args": ["--headless"]}}
EXTERNAL_OWN = {
    "playwright": {"transport": "stdio", "command": "npx", "args": ["--user-data-dir", "{profile_dir}"]}
}


def test_a_pool_on_external_browsers_without_a_profile_placeholder_is_warned() -> None:
    shared = settings(browser={"launch": "external"}, mcp_servers=EXTERNAL_SHARED, serve={"workers": 2})
    own = settings(browser={"launch": "external"}, mcp_servers=EXTERNAL_OWN, serve={"workers": 2})
    single = settings(browser={"launch": "external"}, mcp_servers=EXTERNAL_SHARED)

    assert any("{profile_dir}" in warning for warning in startup_warnings(shared))
    assert startup_warnings(own) == [] and startup_warnings(single) == []
    assert startup_problems(shared) == []  # a warning, not a refusal (P8)


def test_an_unreachable_failure_escalation_is_warned() -> None:
    cascade = {"enabled": True, "tiers": [{"model": "m2"}], "escalate_after_failures": 3}

    assert any("escalate_after_failures" in warning for warning in startup_warnings(settings(cascade=cascade)))
    assert startup_warnings(settings(cascade={**cascade, "escalate_after_failures": 2})) == []
