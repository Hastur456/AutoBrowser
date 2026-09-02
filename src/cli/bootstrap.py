"""Bootstrap helpers that wire CLI arguments into a session runtime."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from typing import Any

from src.providers.ollama import ollama_llm_factory

from src.browser import BrowserProvider
from src.cli.agent_cli import run_cli
from src.cli.output import print_tools
from src.cli.tasks import resolve_initial_task
from src.harness.chrome import start_chrome_cdp, wait_for_port
from src.harness.langsmith import configure_langsmith_tracing
from src.harness.session import SessionConfig, SessionRuntime
from src.mcp.playwright_runtime import close_mcp_session, load_browser_provider


def build_session(
    args: argparse.Namespace,
    *,
    llm_factory: Callable[..., Any] = ollama_llm_factory,
    start_chrome: Callable[[str, str, int], Any] = start_chrome_cdp,
    wait_for_cdp_port: Callable[[int, float], Awaitable[None]] = wait_for_port,
    browser_provider_loader: Callable[[int], Awaitable[BrowserProvider]] = load_browser_provider,
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
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=wait_for_cdp_port,
        load_browser_provider=browser_provider_loader,
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
