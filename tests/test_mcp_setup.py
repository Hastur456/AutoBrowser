from types import SimpleNamespace

import pytest

from src.harness.mcp_setup import build_mcp_runtime

SERVERS = {
    "playwright": {"transport": "stdio", "command": "npx", "stateful": True},
    "web": {"transport": "stdio", "command": "web-browser"},
}


def _settings(**overrides):
    return SimpleNamespace(**{"mcp_servers": SERVERS, "browser_mcp_server": None, **overrides})


def test_browser_server_defaults_to_playwright_entry():
    assert build_mcp_runtime(cdp_port=9222, settings=_settings()).browser_server == "playwright"


def test_browser_server_setting_selects_configured_entry():
    runtime = build_mcp_runtime(cdp_port=9222, settings=_settings(browser_mcp_server="web"))
    assert runtime.browser_server == "web"


def test_unknown_browser_server_fails_fast():
    with pytest.raises(ValueError, match="browser_mcp_server='missing'"):
        build_mcp_runtime(cdp_port=9222, settings=_settings(browser_mcp_server="missing"))


def test_an_external_browser_refuses_servers_that_still_connect_over_cdp():
    cdp = {"transport": "stdio", "command": "npx", "args": ["--cdp-endpoint", "{cdp_endpoint}"]}
    settings = _settings(
        mcp_servers={"playwright": cdp, "web": SERVERS["web"]},
        browser=SimpleNamespace(launch="external"),
    )

    with pytest.raises(ValueError, match="launch: external.*'playwright'"):
        build_mcp_runtime(cdp_port=9222, settings=settings)


def test_an_external_browser_accepts_servers_that_launch_their_own():
    settings = _settings(browser=SimpleNamespace(launch="external"))

    assert build_mcp_runtime(cdp_port=9222, settings=settings).browser_server == "playwright"


def test_cdp_placeholders_are_fine_when_the_session_launches_chrome():
    cdp = {"transport": "stdio", "command": "npx", "args": ["--cdp-endpoint", "{cdp_endpoint}"]}
    settings = _settings(mcp_servers={"playwright": cdp}, browser=SimpleNamespace(launch="cdp"))

    assert build_mcp_runtime(cdp_port=9222, settings=settings).browser_server == "playwright"


def test_no_browser_server_when_playwright_absent():
    settings = _settings(mcp_servers={"web": SERVERS["web"]})
    assert build_mcp_runtime(cdp_port=9222, settings=settings).browser_server is None
