"""Bootstrap helpers that wire CLI arguments into a session runtime."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from typing import Any

from src.providers.ollama import ollama_llm_factory

from src.cli.agent_cli import run_cli
from src.cli.output import print_tools
from src.cli.tasks import resolve_initial_task
from src.harness.chrome import start_chrome_cdp, wait_for_port
from src.harness.session import (
    MCPRuntimeFactory,
    SessionConfig,
    SessionRuntime,
    default_mcp_runtime_factory,
)


def build_session(
    args: argparse.Namespace,
    *,
    llm_factory: Callable[..., Any] = ollama_llm_factory,
    start_chrome: Callable[[str, str, int], Any] = start_chrome_cdp,
    wait_for_cdp_port: Callable[[int, float], Awaitable[None]] = wait_for_port,
    mcp_runtime_factory: MCPRuntimeFactory = default_mcp_runtime_factory,
    tool_printer: Callable[[list[Any]], None] | None = print_tools,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[..., None] = print,
) -> SessionRuntime:
    """Build a process-long session runtime from parsed CLI arguments.

    MCP servers come from ``settings.mcp_servers`` via ``mcp_runtime_factory``; they are
    started together with the session and shut down by ``SessionRuntime.close()``.
    """

    session_config = SessionConfig.from_args(args)
    return SessionRuntime(
        session_config,
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=wait_for_cdp_port,
        mcp_runtime_factory=mcp_runtime_factory,
        print_tools=tool_printer,
        input_fn=input_fn,
        output_fn=output_fn,
    )


async def run_agent(args: argparse.Namespace) -> int:
    """Run the agent with parsed CLI arguments."""

    session = build_session(args)
    try:
        return await session.run_forever(initial_task=resolve_initial_task(args))
    finally:
        await session.close()


def run_agent_cli(args: argparse.Namespace) -> int:
    """Run the cmd2 interactive CLI from a synchronous entry point."""

    session = build_session(args)
    return run_cli(session, initial_task=resolve_initial_task(args))


__all__ = ["build_session", "run_agent", "run_agent_cli"]
