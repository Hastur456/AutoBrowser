#!/usr/bin/env python3
"""Run AutoBrowser Golden Set scenarios from a JSONL file."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.batch import run_batch
from src.cli.bootstrap import build_session
from src.cli.parser import DEFAULT_CDP_PORT, DEFAULT_CHROME_PATH, DEFAULT_USER_DATA_DIR
from src.harness.session import SessionRuntime
from src.llm import DEFAULT_OLLAMA_MODEL

SessionBuilder = Callable[[argparse.Namespace], SessionRuntime]
BatchRunner = Callable[..., Awaitable[dict[str, Any]]]


async def run_batch_from_args(
    args: argparse.Namespace,
    *,
    session_builder: SessionBuilder = build_session,
    batch_runner: BatchRunner = run_batch,
) -> dict[str, Any]:
    """Run the configured batch with a fresh session runtime per scenario."""

    session_factory = _session_factory(args, session_builder)
    return await batch_runner(
        tasks_path=args.tasks,
        session_factory=session_factory,
        continue_on_error=args.continue_on_error,
        config=_batch_config(args),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks",
        type=Path,
        required=True,
        help="Input tasks JSONL path. Each line is one batch scenario.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue running remaining tasks after a task failure.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OLLAMA_MODEL,
        help=f"Ollama model name. Default: {DEFAULT_OLLAMA_MODEL}",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run without loading MCP browser tools.",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=50,
        help="LangGraph recursion limit. Default: 50",
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
    _add_session_defaults(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run_batch_from_args(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _session_factory(
    args: argparse.Namespace,
    session_builder: SessionBuilder,
) -> Callable[[], SessionRuntime]:
    scenario_index = 0
    next_port = int(args.cdp_port)

    def build() -> SessionRuntime:
        nonlocal scenario_index, next_port
        scenario_index += 1
        session_args = copy.copy(args)
        session_args.user_data_dir = _scenario_user_data_dir(
            args.user_data_dir,
            scenario_index,
        )
        if not args.no_mcp:
            session_args.cdp_port = _next_available_port(next_port)
            next_port = int(session_args.cdp_port) + 1
        return session_builder(session_args)

    return build


def _scenario_user_data_dir(user_data_dir: str, scenario_index: int) -> str:
    root = Path(user_data_dir)
    return str(root.parent / f"{root.name}-scenario-{scenario_index}")


def _next_available_port(start: int) -> int:
    port = start
    while _is_port_open(port):
        port += 1
    return port


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("localhost", port)) == 0


def _add_session_defaults(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(
        temperature=0.0,
        show_state=False,
        hide_snapshot=False,
        show_tools=False,
        json=False,
        compress_tools=False,
        cdp_timeout=30.0,
    )


def _batch_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "no_mcp": args.no_mcp,
        "recursion_limit": args.recursion_limit,
        "chrome_path": args.chrome_path,
        "user_data_dir": args.user_data_dir,
        "cdp_port": args.cdp_port,
    }


if __name__ == "__main__":
    raise SystemExit(main())
