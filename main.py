"""Command-line entrypoint for the AutoBrowser agent (``uv run main.py``).

Same entry point as the installed ``autobrowser`` command (``src.cli.entry:main``).
"""

from __future__ import annotations

from src.cli.entry import main

if __name__ == "__main__":
    raise SystemExit(main())
