"""The ``autobrowser`` command (``[project.scripts]``) and ``main.py``'s entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from src.cli.bootstrap import run_agent_cli
from src.cli.parser import build_parser
from src.cli.subcommands import SUBCOMMANDS, run_subcommand


def main(argv: Sequence[str] | None = None) -> int:
    """Run a subcommand (``doctor``, ``sessions``, ``replay``, ``completion``) or the agent.

    ``argv`` defaults to ``sys.argv[1:]``. A task that is literally one of the subcommand
    words is passed with ``--task`` or after ``--``.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in SUBCOMMANDS:
        return run_subcommand(arguments)
    parser = build_parser()
    _autocomplete(parser)
    args = parser.parse_args(arguments)
    return run_agent_cli(args)


def _autocomplete(parser: object) -> None:
    """Answer a shell completion request (only when argcomplete's env vars are set)."""

    try:
        import argcomplete
    except ImportError:  # pragma: no cover - argcomplete is a runtime dependency
        return
    argcomplete.autocomplete(parser)  # type: ignore[arg-type]


__all__ = ["main"]
