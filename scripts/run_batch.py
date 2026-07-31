#!/usr/bin/env python3
"""Run AutoBrowser batch tasks from a JSONL file."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.batch import run_batch
from src.agent.agent import DEFAULT_OLLAMA_MODEL
from src.cli.bootstrap import build_session
from src.cli.parser import DEFAULT_CDP_PORT, DEFAULT_CHROME_PATH, DEFAULT_USER_DATA_DIR
from src.harness.session import SessionRuntime

SessionBuilder = Callable[[argparse.Namespace], SessionRuntime]
BatchRunner = Callable[..., Awaitable[dict[str, Any]]]


async def run_batch_from_args(
    args: argparse.Namespace,
    *,
    session_builder: SessionBuilder = build_session,
    batch_runner: BatchRunner = run_batch,
) -> dict[str, Any]:
    """Build a session runtime from CLI args and run the configured batch."""

    session = session_builder(args)
    config = _batch_config(args, session)
    return await batch_runner(
        tasks_path=args.tasks,
        session=session,
        continue_on_error=args.continue_on_error,
        config=config,
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


def _batch_config(args: argparse.Namespace, session: SessionRuntime) -> dict[str, Any]:
    config = getattr(session, "config", None)
    if config is not None:
        return asdict(config)
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
