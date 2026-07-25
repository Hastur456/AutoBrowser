"""Bootstrap helpers that wire CLI arguments into a session runtime."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_ollama import ChatOllama

from src.agent.agent import build_agent_graph
from src.cli.agent_cli import run_cli
from src.cli.output import print_tools
from src.cli.task_runner import run_task
from src.cli.tasks import resolve_initial_task
from src.harness.chrome import start_chrome_cdp, wait_for_port
from src.harness.langsmith import configure_langsmith_tracing
from src.harness.session import SessionConfig, SessionRuntime
from src.mcp.playwright_runtime import close_mcp_session, load_browser_tools


def build_session(
    args: argparse.Namespace,
    *,
    graph_builder: Callable[..., Any] = build_agent_graph,
    llm_factory: Callable[..., Any] = ChatOllama,
    task_runner: Callable[..., Awaitable[Any]] = run_task,
    start_chrome: Callable[[str, str, int], Any] = start_chrome_cdp,
    wait_for_cdp_port: Callable[[int, float], Awaitable[None]] = wait_for_port,
    browser_tool_loader: Callable[[int], Awaitable[Sequence[Any]]] = load_browser_tools,
    close_mcp: Callable[[], Awaitable[None]] = close_mcp_session,
    tool_printer: Callable[[list[Any]], None] | None = print_tools,
    tracing_configurator: Callable[[], bool] = configure_langsmith_tracing,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[..., None] = print,
) -> SessionRuntime:
    """Build a process-long session runtime from parsed CLI arguments."""

    tracing_enabled = tracing_configurator()
    session_config = SessionConfig.from_args(args, tracing_enabled=tracing_enabled)
    return SessionRuntime(
        session_config,
        graph_builder=graph_builder,
        llm_factory=llm_factory,
        task_runner=task_runner,
        start_chrome_cdp=start_chrome,
        wait_for_port=wait_for_cdp_port,
        load_browser_tools=browser_tool_loader,
        close_mcp_session=close_mcp,
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
