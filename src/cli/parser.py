"""Argument parser for the AutoBrowser CLI."""

from __future__ import annotations

import argparse
import os

from src.agent.agent import DEFAULT_OLLAMA_MODEL

DEFAULT_CDP_PORT = int(os.getenv("PORT", "9222"))
DEFAULT_CHROME_PATH = os.getenv(
    "CHROME_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
DEFAULT_USER_DATA_DIR = os.getenv("USER_DATA_DIR", r"C:\temp\chrome_debug_profile")


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(
        description="Run the AutoBrowser LangGraph agent from the command line."
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
        default=DEFAULT_OLLAMA_MODEL,
        help=f"Ollama model name. Default: {DEFAULT_OLLAMA_MODEL}",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM temperature. Default: 0",
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
        default=_env_flag("AUTOBROWSER_AGENT_LOOP"),
        help="Use the explicit AgentLoopEngine shell. Default: AUTOBROWSER_AGENT_LOOP.",
    )
    parser.add_argument(
        "--chrome-path",
        default=DEFAULT_CHROME_PATH,
        help="Path to Chrome executable. Defaults to CHROME_PATH from .env.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=DEFAULT_USER_DATA_DIR,
        help="Chrome user data directory. Defaults to USER_DATA_DIR from .env.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"Chrome DevTools Protocol port. Default: {DEFAULT_CDP_PORT}",
    )
    parser.add_argument(
        "--cdp-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for Chrome CDP port. Default: 30",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=50,
        help="LangGraph recursion limit. Default: 25",
    )
    return parser


__all__ = [
    "DEFAULT_CDP_PORT",
    "DEFAULT_CHROME_PATH",
    "DEFAULT_USER_DATA_DIR",
    "build_parser",
]
