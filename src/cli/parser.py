"""Argument parser for the AutoBrowser CLI.

Every default is read from :mod:`src.config` at parse time, so CLI flags override
the configured value only when explicitly supplied.
"""

from __future__ import annotations

import argparse

from src.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    settings = get_settings()

    parser = argparse.ArgumentParser(
        description="Run the AutoBrowser browser agent from the command line."
    )
    parser.add_argument("task", nargs="*", help="Task for the browser agent.")
    parser.add_argument("--task", "-t", dest="task_text", help="Task for the agent.")
    parser.add_argument(
        "--loop",
        "-l",
        action="store_true",
        help="Run interactive loop mode. Exit with quit, exit, or выход.",
    )
    parser.add_argument(
        "--model",
        default=settings.llm.model,
        help=f"Ollama model name. Default: {settings.llm.model}",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=settings.llm.temperature,
        help=f"LLM temperature. Default: {settings.llm.temperature}",
    )
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="Print state updates after each graph node runs.",
    )
    parser.add_argument(
        "--hide-snapshot",
        action="store_true",
        help="Redact browser snapshots from printed state updates.",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        help="Print MCP tool names after loading them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print state as JSON instead of a readable Python representation.",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run without loading MCP tools. Useful for dry CLI checks.",
    )
    parser.add_argument(
        "--compress-tools",
        action="store_true",
        help="Compress tool outputs and snapshots with the observer LLM.",
    )
    parser.add_argument(
        "--agent-loop",
        dest="agent_loop",
        action=argparse.BooleanOptionalAction,
        default=settings.flags.agent_loop,
        help=(
            "Use the explicit AgentLoopEngine shell. Default: "
            "AUTOBROWSER_FLAGS__AGENT_LOOP."
        ),
    )
    parser.add_argument(
        "--chrome-path",
        default=str(settings.browser.chrome_path),
        help=(
            "Path to Chrome executable. Defaults to "
            "AUTOBROWSER_BROWSER__CHROME_PATH."
        ),
    )
    parser.add_argument(
        "--user-data-dir",
        default=str(settings.browser.user_data_dir),
        help=(
            "Chrome user data directory. Defaults to "
            "AUTOBROWSER_BROWSER__USER_DATA_DIR."
        ),
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=settings.browser.cdp_port,
        help=f"Chrome DevTools Protocol port. Default: {settings.browser.cdp_port}",
    )
    parser.add_argument(
        "--cdp-timeout",
        type=float,
        default=settings.browser.cdp_timeout_seconds,
        help=(
            "Seconds to wait for Chrome CDP port. "
            f"Default: {settings.browser.cdp_timeout_seconds}"
        ),
    )
    parser.add_argument(
        "--turn-cap",
        type=int,
        default=settings.loop.turn_cap,
        help=f"Maximum agent turns (turn cap). Default: {settings.loop.turn_cap}",
    )
    return parser


__all__ = ["build_parser"]
