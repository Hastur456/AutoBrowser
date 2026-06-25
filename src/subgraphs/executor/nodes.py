"""Nodes of executor subgraph"""


import asyncio
import random
from langchain.messages import (
    ToolMessage,
)

from . import ExecutorState


async def mcp_invoke_node(state: ExecutorState, tools):
    """Invoke mcp tools"""
    messages = state.get("messages", [])
    if not messages:
        return state

    last_message = messages[-1]
    tool_calls = getattr(last_message, "tool_calls", []) or []
    if not tool_calls:
        return state

    error_count = state.get("error_count", 0)
    retry_attempts = state.get("retry_attempts", 0)
    total_tool_calls = state.get("total_tool_calls", 0)

    responses = []
    tool_registry = {t.name: t for t in tools}

    had_error = False
    fatal_error = False

    for tool_call in tool_calls:
        total_tool_calls += 1

        tool_name = tool_call.get("name")
        tool_id = tool_call.get("id")
        tool_args = tool_call.get("args", {})

        tool = tool_registry.get(tool_name)

        if not tool:
            fatal_error = True
            had_error = True

            responses.append(
                ToolMessage(
                    content=f"Tool '{tool_name}' not found",
                    tool_call_id=tool_id,
                )
            )
            continue

        try:
            result = await tool.with_retry(
                stop_after_attempt=3,
                wait_exponential_jitter=True,
            ).ainvoke(tool_args)

            responses.append(
                ToolMessage(
                    content=str(result),
                    tool_call_id=tool_id,
                )
            )

        except (TimeoutError, ConnectionError) as exc:
            had_error = True

            responses.append(
                ToolMessage(
                    content=f"Temporary error: {str(exc)}",
                    tool_call_id=tool_id,
                )
            )

        except RuntimeError as exc:
            had_error = True

            responses.append(
                ToolMessage(
                    content=f"Runtime error: {str(exc)}",
                    tool_call_id=tool_id,
                )
            )

    if fatal_error:
        return {
            "messages": responses,
            "last_action": responses[-1],
            "error_count": error_count + 1,
            "retry_attempts": retry_attempts,
            "total_tool_calls": total_tool_calls,
            "last_error_type": "fatal",
        }

    if had_error:
        return {
            "messages": responses,
            "last_action": responses[-1],
            "error_count": error_count + 1,
            "retry_attempts": retry_attempts + 1,
            "total_tool_calls": total_tool_calls,
            "last_error_type": "retryable",
        }

    return {
        "messages": responses,
        "last_action": responses[-1],
        "error_count": 0,
        "retry_attempts": 0,
        "total_tool_calls": total_tool_calls,
        "last_error_type": None,
    }


async def backoff_node(state: ExecutorState):
    """Invoke backoff functional after exception in mcp_invoke node"""
    attempt = state.get("retry_attempts", 1)

    base_delay = 2 ** attempt
    jitter = random.uniform(0, 0.5)

    delay = min(base_delay + jitter, 10)

    await asyncio.sleep(delay)

    return state
