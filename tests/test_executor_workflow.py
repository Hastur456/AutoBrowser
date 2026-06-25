import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain.messages import HumanMessage, AIMessage, ToolMessage
from langgraph.graph import END

from src.subgraphs.executor.routers import retry_router
from src.subgraphs.executor.workflow import ExecutorWorkflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(
    last_error_type=None,
    retry_attempts=0,
    error_count=0,
    total_tool_calls=0,
    messages=None,
    last_action=None,
):
    return {
        "messages": messages or [],
        "error_count": error_count,
        "retry_attempts": retry_attempts,
        "total_tool_calls": total_tool_calls,
        "last_error_type": last_error_type,
        "last_action": last_action,
    }


def ai_message_with_tool_call(tool_name="browser_navigate", args=None, call_id="c1"):
    msg = AIMessage(content="")
    msg.tool_calls = [{"name": tool_name, "id": call_id, "args": args or {"url": "https://example.com"}}]
    return msg


# ---------------------------------------------------------------------------
# retry_router unit tests
# ---------------------------------------------------------------------------

def test_retry_router_fatal_returns_abort():
    assert retry_router(make_state(last_error_type="fatal"), max_retries=3) == "abort"


def test_retry_router_retryable_under_limit_returns_backoff():
    assert retry_router(make_state(last_error_type="retryable", retry_attempts=1), max_retries=3) == "backoff"


def test_retry_router_retryable_at_limit_returns_abort():
    assert retry_router(make_state(last_error_type="retryable", retry_attempts=3), max_retries=3) == "abort"


def test_retry_router_no_error_returns_abort():
    assert retry_router(make_state(last_error_type=None), max_retries=3) == "abort"


# ---------------------------------------------------------------------------
# ExecutorWorkflow integration tests (mocked mcp_invoke_node + backoff_node)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_success_exits_on_first_call():
    """Tool succeeds → workflow ends after one mcp call."""
    tool = MagicMock()
    tool.name = "browser_navigate"
    tool.with_retry = MagicMock(return_value=tool)
    tool.ainvoke = AsyncMock(return_value="navigated")

    msg = ai_message_with_tool_call()
    state = make_state(messages=[msg])

    workflow = ExecutorWorkflow(tools=[tool], max_retries=3)
    result = await workflow.run(state)

    assert result["last_error_type"] is None
    assert result["total_tool_calls"] == 1


@pytest.mark.asyncio
async def test_workflow_unknown_tool_is_fatal():
    """Unknown tool name → fatal, workflow aborts immediately."""
    state = make_state(messages=[ai_message_with_tool_call(tool_name="nonexistent")])

    workflow = ExecutorWorkflow(tools=[], max_retries=3)
    result = await workflow.run(state)

    assert result["last_error_type"] == "fatal"


@pytest.mark.asyncio
async def test_workflow_retryable_error_retries_then_succeeds():
    """
    First call raises RuntimeError (retryable), second call succeeds.
    Workflow should go: mcp → backoff → mcp → abort(success).
    """
    call_count = 0

    async def fake_ainvoke(args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("CDP timeout")
        return "ok"

    tool = MagicMock()
    tool.name = "browser_navigate"
    tool.with_retry = MagicMock(return_value=tool)
    tool.ainvoke = fake_ainvoke

    msg = ai_message_with_tool_call()

    with patch("src.subgraphs.executor.nodes.asyncio.sleep", new_callable=AsyncMock):
        workflow = ExecutorWorkflow(tools=[tool], max_retries=3)
        result = await workflow.run(make_state(messages=[msg]))

    assert result["last_error_type"] is None
    assert call_count == 2


@pytest.mark.asyncio
async def test_workflow_retryable_error_exhausts_retries():
    """All calls raise RuntimeError → retries exhausted → abort."""
    tool = MagicMock()
    tool.name = "browser_navigate"
    tool.with_retry = MagicMock(return_value=tool)
    tool.ainvoke = AsyncMock(side_effect=RuntimeError("persistent failure"))

    msg = ai_message_with_tool_call()

    with patch("src.subgraphs.executor.nodes.asyncio.sleep", new_callable=AsyncMock):
        workflow = ExecutorWorkflow(tools=[tool], max_retries=2)
        result = await workflow.run(make_state(messages=[msg]))

    assert result["last_error_type"] == "retryable"
    assert result["retry_attempts"] >= 2


@pytest.mark.asyncio
async def test_workflow_no_tool_calls_returns_state_unchanged():
    """Message with no tool_calls → mcp_invoke_node returns state as-is."""
    state = make_state(messages=[HumanMessage(content="hello")])
    workflow = ExecutorWorkflow(tools=[], max_retries=3)
    result = await workflow.run(state)

    assert result["last_error_type"] is None
    assert result["total_tool_calls"] == 0
