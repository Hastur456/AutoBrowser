"""ci/regression.yaml: the CI profile of the live runs loads and decides what it must.

A typo in the profile would otherwise surface only in a live run on GitHub; here it fails the
offline gate (the settings sections are ``extra="forbid"``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import config

PROFILE = Path(__file__).resolve().parents[1] / "ci" / "regression.yaml"


@pytest.fixture
def ci_settings(monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    monkeypatch.setenv(config.CONFIG_FILE_ENV_VAR, str(PROFILE))
    config.get_settings.cache_clear()
    return config.get_settings()


def test_the_profile_runs_a_headless_browser_the_mcp_server_launches(ci_settings: config.Settings) -> None:
    assert ci_settings.browser.launch == "external"
    server = ci_settings.mcp_servers["playwright"]
    assert "--headless" in server.args
    assert "{profile_dir}" in server.args


def test_nobody_answers_approvals_and_no_trace_leaves_the_runner(ci_settings: config.Settings) -> None:
    assert ci_settings.permissions.mode == "dont_ask"
    assert ci_settings.telemetry.otlp_enabled is False


def test_langfuse_stays_off_and_without_content_unless_the_workflow_enables_it(
    ci_settings: config.Settings,
) -> None:
    platform = ci_settings.eval_platform
    assert platform.enabled is False
    assert platform.capture_content == "none"
    assert platform.environment == "ci-main"
    assert platform.secret_key is None


def test_the_workflow_enables_langfuse_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.CONFIG_FILE_ENV_VAR, str(PROFILE))
    monkeypatch.setenv("AUTOBROWSER_EVAL_PLATFORM__ENABLED", "true")
    config.get_settings.cache_clear()

    assert config.get_settings().eval_platform.enabled is True
