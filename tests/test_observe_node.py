import pytest
from unittest.mock import AsyncMock, MagicMock

from src.agent.nodes import observe_node


def make_state():
    return {
        "messages": [],
        "error_count": 0,
        "retry_attempts": 0,
        "total_tool_calls": 0,
        "last_error_type": None,
        "last_action": None,
        "observation": None,
        "reflection": None,
        "replan_count": 0,
    }


@pytest.mark.asyncio
async def test_observe_returns_snapshot():
    tool = MagicMock()
    tool.name = "browser_snapshot"
    tool.ainvoke = AsyncMock(return_value="<snapshot>page content</snapshot>")

    result = await observe_node(make_state(), tools=[tool])

    assert result == {"observation": "<snapshot>page content</snapshot>"}


@pytest.mark.asyncio
async def test_observe_no_snapshot_tool():
    result = await observe_node(make_state(), tools=[])
    assert result == {"observation": None}


@pytest.mark.asyncio
async def test_observe_snapshot_error():
    tool = MagicMock()
    tool.name = "browser_snapshot"
    tool.ainvoke = AsyncMock(side_effect=RuntimeError("CDP timeout"))

    result = await observe_node(make_state(), tools=[tool])

    assert result["observation"].startswith("snapshot_error:")
