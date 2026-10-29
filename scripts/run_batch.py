#!/usr/bin/env python3
"""Run AutoBrowser Golden Set scenarios from a JSONL file.

``--grade`` grades the final answer of every scenario's last task against its ``expected``
mapping (src/agent_loop/graders.py) and exits 1 when a grade or a scenario fails; use it with
``--continue-on-error`` so every scenario gets a grade. Without ``--continue-on-error`` the
first scenario that raises stops the batch with a traceback (exit 1, no summary); otherwise,
without ``--grade``, the exit code is 0.

Each scenario runs in a fresh Chrome profile under the batch directory
(``<storage.batches_dir>/<batch_id>/profiles/scenario-N``), so no cookie or login carries over
from another scenario or another run; the profiles are deleted when the batch ends.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.batch import BatchScenario, run_batch, settings_provenance
from src.cli.bootstrap import build_session
from src.config import get_settings
from src.harness.session import SessionRuntime

#: Subdirectory of a batch directory holding the scenarios' Chrome profiles.
PROFILES_DIR_NAME = "profiles"

SessionBuilder = Callable[[argparse.Namespace], SessionRuntime]
BatchRunner = Callable[..., Awaitable[dict[str, Any]]]


async def run_batch_from_args(
    args: argparse.Namespace,
    *,
    session_builder: SessionBuilder = build_session,
    batch_runner: BatchRunner = run_batch,
) -> dict[str, Any]:
    """Run the configured batch with a fresh session runtime and Chrome profile per scenario."""

    batch_id = f"batch-{uuid4().hex}"
    profiles_dir = get_settings().storage.batches_dir / batch_id / PROFILES_DIR_NAME
    provenance = _provenance(args)
    commit = provenance["git"]["commit"]
    labels = {
        "autobrowser.batch.id": batch_id,
        **({"vcs.ref.head.revision": commit} if commit else {}),
    }
    session_factory = _session_factory(args, session_builder, profiles_dir, labels)
    try:
        return await batch_runner(
            tasks_path=args.tasks,
            session_factory=session_factory,
            batch_id=batch_id,
            continue_on_error=args.continue_on_error,
            config=_batch_config(args),
            grade=args.grade,
            provenance=provenance,
        )
    finally:
        # The profiles are scratch state; a file Chrome still holds stays in the batch dir.
        shutil.rmtree(profiles_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()

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
        "--grade",
        action="store_true",
        help="Grade each scenario's final answer against its expected mapping; exit 1 on a failure.",
    )
    parser.add_argument(
        "--model",
        default=settings.llm.model,
        help=f"Ollama model name. Default: {settings.llm.model}",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run without loading MCP browser tools.",
    )
    parser.add_argument(
        "--turn-cap",
        type=int,
        default=settings.loop.turn_cap,
        help=f"Maximum agent turns (turn cap). Default: {settings.loop.turn_cap}",
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
        "--cdp-port",
        type=int,
        default=settings.browser.cdp_port,
        help=f"Chrome DevTools Protocol port. Default: {settings.browser.cdp_port}",
    )
    _add_session_defaults(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run_batch_from_args(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.grade and (result.get("status") == "failed" or result.get("grades", {}).get("failed")):
        return 1
    return 0


def _session_factory(
    args: argparse.Namespace,
    session_builder: SessionBuilder,
    profiles_dir: Path,
    labels: Mapping[str, str],
) -> Callable[[BatchScenario], SessionRuntime]:
    scenario_index = 0
    next_port = int(args.cdp_port)

    def build(scenario: BatchScenario) -> SessionRuntime:
        nonlocal scenario_index, next_port
        scenario_index += 1
        session_args = copy.copy(args)
        session_args.user_data_dir = str(profiles_dir / f"scenario-{scenario_index}")
        session_args.labels = {**labels, "autobrowser.scenario.id": scenario.scenario_id}
        if not args.no_mcp:
            session_args.cdp_port = _next_available_port(next_port)
            next_port = int(session_args.cdp_port) + 1
        return session_builder(session_args)

    return build


def _provenance(args: argparse.Namespace) -> dict[str, Any]:
    """What makes two runs comparable: commit, model, Golden Set and settings fingerprints."""

    settings = get_settings()
    return {
        "git": _git_info(),
        "model": args.model,
        "provider": getattr(args, "provider", None) or settings.llm.provider,
        "tasks_sha256": _tasks_sha256(Path(args.tasks)),
        **settings_provenance(settings),
        "python": platform.python_version(),
        "platform": sys.platform,
    }


def _git_info() -> dict[str, Any]:
    """Commit, branch and dirty flag of the checkout; all ``None`` without git."""

    def git(*argv: str) -> str:
        completed = subprocess.run(
            ["git", *argv], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    try:
        return {
            "commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "branch": None, "dirty": None}


def _tasks_sha256(path: Path) -> str:
    """Hash of the tasks file, the same for a Windows (CRLF) and a Linux (LF) checkout."""

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


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
    settings = get_settings()
    parser.set_defaults(
        temperature=settings.llm.temperature,
        show_state=False,
        show_tools=False,
        json=False,
        compress_tools=False,
        # Inert since the engine-native loop is the only runtime, but SessionConfig reads it.
        agent_loop=settings.flags.agent_loop,
        cdp_timeout=settings.browser.cdp_timeout_seconds,
        # Nobody answers approvals in a batch: a configured ``default`` mode becomes
        # ``dont_ask`` (approvals are non-terminal denies), even when run from a terminal.
        interactive=False,
        permission_mode=None,
    )


def _batch_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "no_mcp": args.no_mcp,
        "turn_cap": args.turn_cap,
        "chrome_path": args.chrome_path,
        "cdp_port": args.cdp_port,
    }


if __name__ == "__main__":
    raise SystemExit(main())
