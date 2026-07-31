#!/usr/bin/env python3
"""Print a compact action sequence from an AutoBrowser JSONL trace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.replay import load_events, print_action_sequence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="Path to events.jsonl")
    args = parser.parse_args()
    print(print_action_sequence(load_events(args.trace)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
