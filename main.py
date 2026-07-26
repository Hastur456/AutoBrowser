"""Command-line entrypoint for the AutoBrowser agent."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
from typing import Any

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

from src.agent.agent import DEFAULT_OLLAMA_MODEL, build_agent_graph
from src.cli.agent_cli import run_cli
from src.cli.bootstrap import build_session as _build_session
from src.cli.output import (
    SNAPSHOT_REDACTION,
    _redact_snapshot_value,
    format_state,
    print_final_state as _print_final_state,
    print_step,
    print_tools,
)
from src.cli.parser import (
    DEFAULT_CDP_PORT,
    DEFAULT_CHROME_PATH,
    DEFAULT_USER_DATA_DIR,
    build_parser,
)
from src.cli.task_runner import run_task
from src.cli.tasks import resolve_initial_task, resolve_task
from src.harness.langsmith import DEFAULT_LANGSMITH_PROJECT, configure_langsmith_tracing
from src.mcp.playwright_runtime import (
    close_mcp_session,
    load_browser_provider,
)

load_dotenv()


def is_port_open(port: int) -> bool:
    """Return whether a localhost TCP port is accepting connections."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("localhost", port)) == 0


def start_chrome_cdp(
    chrome_path: str,
    user_data_dir: str,
    port: int,
) -> subprocess.Popen[Any] | None:
    """Start Chrome with CDP enabled unless the port is already open."""

    if is_port_open(port):
        return None

    if not chrome_path:
        raise RuntimeError("CHROME_PATH is not set. Pass --chrome-path or set it in .env.")
    if not user_data_dir:
        raise RuntimeError(
            "USER_DATA_DIR is not set. Pass --user-data-dir or set it in .env."
        )

    return subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )


async def wait_for_port(port: int, timeout_seconds: float) -> None:
    """Wait until a localhost TCP port opens."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not is_port_open(port):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Chrome CDP port {port} did not open in time.")
        print("Подключение к серверу...")
        await asyncio.sleep(0.5)


def build_session(args: argparse.Namespace):
    """Build a process-long session runtime from parsed CLI arguments."""

    input_fn = globals().get("input", input)
    return _build_session(
        args,
        graph_builder=build_agent_graph,
        llm_factory=ChatOllama,
        task_runner=run_task,
        start_chrome=start_chrome_cdp,
        wait_for_cdp_port=wait_for_port,
        browser_provider_loader=load_browser_provider,
        close_mcp=close_mcp_session,
        tool_printer=print_tools,
        tracing_configurator=configure_langsmith_tracing,
        input_fn=input_fn,
        output_fn=print,
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


def main() -> int:
    """Parse CLI arguments and run the interactive agent CLI."""

    parser = build_parser()
    args = parser.parse_args()
    return run_agent_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
