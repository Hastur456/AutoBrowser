"""The ``autobrowser`` command (``[project.scripts]``) and ``main.py``'s entry point."""

from __future__ import annotations

from collections.abc import Sequence

from src.cli.bootstrap import run_agent_cli
from src.cli.parser import build_parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line (``sys.argv`` when ``argv`` is ``None``) and run the agent CLI."""

    args = build_parser().parse_args(argv)
    return run_agent_cli(args)


__all__ = ["main"]
