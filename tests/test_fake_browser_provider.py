from __future__ import annotations

import pytest

from src.agent.subgraphs.executor.nodes import create_executor_node
from src.browser import (
    BROWSER_ERROR_ACTION_FAILED,
    BROWSER_ERROR_INVALID_REF,
    FakeBrowserProvider,
)


def test_fake_provider_rejects_empty_snapshots() -> None:
    with pytest.raises(
        ValueError, match="FakeBrowserProvider requires at least one snapshot."
    ):
        FakeBrowserProvider([])


@pytest.mark.asyncio
async def test_fake_provider_exposes_all_browser_tools() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])

    tools = await provider.get_tools()

    assert sorted(tool.name for tool in tools) == [
        "browser_click",
        "browser_evaluate",
        "browser_hover",
        "browser_navigate",
        "browser_snapshot",
        "browser_type",
    ]


def test_fake_provider_maps_canonical_requests_to_runtime_tool_names() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])

    click_request = provider.normalize_request(
        {"name": "browser.click", "args": {"target": "e14"}},
        {},
    )
    snapshot_request = provider.normalize_request(
        {"name": "browser.snapshot", "args": {"depth": 2}},
        {},
    )

    assert click_request["name"] == "browser_click"
    assert click_request["args"] == {"target": "e14", "ref": "e14"}
    assert snapshot_request["name"] == "browser_snapshot"
    assert snapshot_request["args"] == {"depth": 2}


@pytest.mark.asyncio
async def test_fake_provider_advances_snapshots_after_successful_actions() -> None:
    first_snapshot = '- button "Catalog" ref=e14'
    second_snapshot = '- heading "Catalog" ref=e21'
    provider = FakeBrowserProvider([first_snapshot, second_snapshot])
    tools_by_name = {tool.name: tool for tool in await provider.get_tools()}

    assert await tools_by_name["browser_snapshot"].ainvoke({}) == first_snapshot
    assert await tools_by_name["browser_click"].ainvoke({"ref": "e14"}) == "Clicked ref e14."
    assert await tools_by_name["browser_snapshot"].ainvoke({}) == second_snapshot


@pytest.mark.asyncio
async def test_fake_provider_evaluate_does_not_advance_snapshot() -> None:
    first_snapshot = '- button "Catalog" ref=e14'
    second_snapshot = '- heading "Catalog" ref=e21'
    provider = FakeBrowserProvider([first_snapshot, second_snapshot])
    tools_by_name = {tool.name: tool for tool in await provider.get_tools()}

    result = await tools_by_name["browser_evaluate"].ainvoke(
        {"expression": "document.title"}
    )

    assert result == {
        "source": "expression",
        "expression": "document.title",
        "snapshot": first_snapshot,
    }
    assert await tools_by_name["browser_snapshot"].ainvoke({}) == first_snapshot
    assert second_snapshot != first_snapshot


@pytest.mark.asyncio
async def test_fake_provider_invalid_ref_is_normalized_by_executor() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])
    node = create_executor_node(browser_providers=[provider])

    result = await node({"tool_request": {"name": "browser.click", "args": {"ref": "e99"}}})

    assert result["tool_result"]["status"] == "error"
    assert result["tool_result"]["error"] == "Ref e99 not found"
    assert result["tool_result"]["error_code"] == BROWSER_ERROR_INVALID_REF


@pytest.mark.asyncio
async def test_fake_provider_normalizes_other_failures_to_action_failed() -> None:
    provider = FakeBrowserProvider(['- button "Catalog" ref=e14'])
    node = create_executor_node(browser_providers=[provider])

    result = await node(
        {"tool_request": {"name": "browser.click", "args": {"target": "catalog button"}}}
    )

    assert result["tool_result"]["status"] == "error"
    assert (
        result["tool_result"]["error"]
        == "Fake browser action requires a ref or ref-like target."
    )
    assert result["tool_result"]["error_code"] == BROWSER_ERROR_ACTION_FAILED
