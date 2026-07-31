#!/usr/bin/env python3
"""Export an AutoBrowser JSONL trace as compact JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.replay import (
    iter_action_sequence,
    load_events,
    summarize_trace,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="Path to events.jsonl")
    args = parser.parse_args()
    events = load_events(args.trace)
    payload = {
        "summary": summarize_trace(events).__dict__,
        "actions": [action.__dict__ for action in iter_action_sequence(events)],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
