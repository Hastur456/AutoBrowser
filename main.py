"""Command-line entrypoint for the AutoBrowser agent."""

from __future__ import annotations

from dotenv import load_dotenv

from src.cli.bootstrap import run_agent_cli
from src.cli.parser import build_parser

load_dotenv()


def main() -> int:
    """Parse CLI arguments and run the interactive agent CLI."""

    parser = build_parser()
    args = parser.parse_args()
    return run_agent_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
