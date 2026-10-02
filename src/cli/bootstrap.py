"""Bootstrap helpers that wire CLI arguments into a session runtime."""

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from src.providers.ollama import ollama_llm_factory

from src.agent_loop.engine import HumanInputCallback
from src.cli.agent_cli import run_cli
from src.cli.approval import ApprovalPrompt
from src.cli.output import print_tools
from src.cli.tasks import resolve_initial_task
from src.config import get_settings
from src.harness.chrome import start_chrome_cdp, wait_for_port
from src.harness.session import (
    MCPRuntimeFactory,
    SessionConfig,
    SessionRuntime,
    default_mcp_runtime_factory,
)


def is_interactive(args: argparse.Namespace) -> bool:
    """Can a human answer approval prompts? ``args.interactive`` (batch: ``False``) or a TTY."""

    explicit = getattr(args, "interactive", None)
    if explicit is not None:
        return bool(explicit)
    return sys.stdin.isatty()


def effective_permission_mode(args: argparse.Namespace, *, interactive: bool) -> str | None:
    """``--permission-mode`` wins; without a human a configured ``default`` becomes ``dont_ask``.

    ``None`` keeps ``settings.permissions.mode``. ``dont_ask`` turns approvals into
    non-terminal denies instead of ending every task that hits one.
    """

    explicit = getattr(args, "permission_mode", None)
    if explicit:
        return str(explicit)
    if not interactive and get_settings().permissions.mode == "default":
        return "dont_ask"
    return None


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
    human_input: HumanInputCallback | None = None,
) -> SessionRuntime:
    """Build a process-long session runtime from parsed CLI arguments.

    MCP servers come from ``settings.mcp_servers`` via ``mcp_runtime_factory``; they are
    started together with the session and shut down by ``SessionRuntime.close()``.
    ``human_input`` answers permission approvals; without it they are denied.
    """

    args = copy.copy(args)
    args.permission_mode = effective_permission_mode(args, interactive=is_interactive(args))
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
        human_input=human_input,
    )


async def run_agent(args: argparse.Namespace) -> int:
    """Run the agent with parsed CLI arguments."""

    session = build_session(args)
    try:
        return await session.run_forever(initial_task=resolve_initial_task(args))
    finally:
        await session.close()


def run_agent_cli(args: argparse.Namespace) -> int:
    """Run the cmd2 interactive CLI from a synchronous entry point.

    With a terminal, permission approvals are asked interactively (``y`` once, ``s`` for the
    session, ``n`` deny); without one they are denied (``dont_ask``).
    """

    approvals = ApprovalPrompt() if is_interactive(args) else None
    session = build_session(args, human_input=approvals)
    return run_cli(session, initial_task=resolve_initial_task(args), approvals=approvals)


__all__ = [
    "build_session",
    "effective_permission_mode",
    "is_interactive",
    "run_agent",
    "run_agent_cli",
]
