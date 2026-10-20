"""The ``autobrowser`` command (``[project.scripts]``) and ``main.py``'s entry point."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from src.cli.bootstrap import run_agent_cli
from src.cli.parser import build_parser
from src.cli.subcommands import SUBCOMMANDS, build_subcommand_parser, run_subcommand


def main(argv: Sequence[str] | None = None) -> int:
    """Run a subcommand (``doctor``, ``sessions``, ``replay``, ``completion``) or the agent.

    ``argv`` defaults to ``sys.argv[1:]``. A task that is literally one of the subcommand
    words is passed with ``--task`` or after ``--``.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    _autocomplete()
    if arguments and arguments[0] in SUBCOMMANDS:
        return run_subcommand(arguments)
    parser = build_parser()
    args = parser.parse_args(arguments)
    return run_agent_cli(args)


def _autocomplete() -> None:
    """Answer a shell completion request and exit (only under argcomplete's env vars).

    During completion the program gets no argv, so the line being completed
    (``COMP_LINE``) decides which parser answers: a subcommand's or the main one.
    """

    if "_ARGCOMPLETE" not in os.environ:
        return
    import argcomplete

    words = os.environ.get("COMP_LINE", "").split()
    parser = build_subcommand_parser() if len(words) > 1 and words[1] in SUBCOMMANDS else build_parser()
    argcomplete.autocomplete(parser)


__all__ = ["main"]
