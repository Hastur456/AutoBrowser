#!/usr/bin/env python3
"""Export AutoBrowser session task rows as JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.export import collect_session_export_rows


def export_sessions(
    *,
    sessions_dir: Path,
    out_path: Path,
    feedback_path: Path | None = None,
) -> int:
    """Write one JSON object per exported task row and return the row count."""

    rows = collect_session_export_rows(sessions_dir, feedback_path=feedback_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    content = "\n".join(lines)
    if content:
        content += "\n"
    out_path.write_text(content, encoding="utf-8")
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path(".autobrowser") / "sessions",
        help="Directory containing .autobrowser session subdirectories.",
    )
    parser.add_argument(
        "--feedback",
        type=Path,
        default=None,
        help="Optional feedback JSONL path. Defaults to <sessions-dir>/../feedback.jsonl.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output JSONL path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    count = export_sessions(
        sessions_dir=args.sessions_dir,
        feedback_path=args.feedback,
        out_path=args.out,
    )
    print(f"Exported {count} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
