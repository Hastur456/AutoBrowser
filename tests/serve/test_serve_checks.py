"""What stops the stand from starting, before any browser runs."""

from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.serve.checks import startup_problems, trigger_secret
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
