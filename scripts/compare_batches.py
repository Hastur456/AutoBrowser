#!/usr/bin/env python3
"""Compare two Golden Set batch runs and give a verdict.

Without arguments the latest run in ``<storage.batches_dir>`` meets the previous run of the
same Golden Set; with two arguments (batch ids or directories) the second is the newer one.
Prints one line per finding (``--json`` for machine output) and writes ``compare.md`` into the
newer batch directory.

Exit codes: 0 no regression; 1 at least one regression; 2 unreadable input, different Golden
Sets or no previous comparable run; 3 only slower/costlier findings under ``--perf-gate``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_loop.batch import tasks_sha256
from src.agent_loop.batch_compare import compare_batches
from src.config import get_settings

COMPARE_FILE_NAME = "compare.md"
BATCH_FILE_NAME = "batch.json"
RUN_INDEX_FILE_NAME = "run_index.jsonl"
TASKS_COPY_FILE_NAME = "tasks.jsonl"

Batch = tuple[Path, dict[str, Any], list[dict[str, Any]]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_a", nargs="?", help="The older run: a batch id or directory.")
    parser.add_argument("batch_b", nargs="?", help="The newer run: a batch id or directory.")
    parser.add_argument("--json", action="store_true", help="Print the comparison as JSON.")
    parser.add_argument(
        "--perf-gate",
        action="store_true",
        help="Exit 3 when the only findings are slower/costlier scenarios.",
    )
    parser.add_argument(
        "--batches-dir",
        type=Path,
        default=None,
        help="Where batch ids are looked up. Default: storage.batches_dir.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    batches_dir = args.batches_dir or settings.storage.batches_dir
    if bool(args.batch_a) != bool(args.batch_b):
        print("error: name both batches or neither", file=sys.stderr)
        return 2
    try:
        if args.batch_a:
            older = load_batch(_resolve(batches_dir, args.batch_a))
            newer = load_batch(_resolve(batches_dir, args.batch_b))
        else:
            pair = _latest_pair(Path(batches_dir))
            if pair is None:
                print(f"error: no previous comparable run in {batches_dir}", file=sys.stderr)
                return 2
            older, newer = pair
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    comparison = compare_batches(older[1], older[2], newer[1], newer[2], settings.pipeline)
    if not comparison.comparable:
        print(
            f"error: {comparison.batch_a} and {comparison.batch_b} are not comparable"
            " (different Golden Sets)",
            file=sys.stderr,
        )
        return 2
    (newer[0] / COMPARE_FILE_NAME).write_text(comparison.render_markdown(), encoding="utf-8")
    if args.json:
        print(json.dumps(comparison.to_json_dict(args.perf_gate), ensure_ascii=False, indent=2))
    else:
        print(f"compare: {comparison.batch_a} -> {comparison.batch_b}")
        for warning in comparison.warnings:
            print(f"warning: {warning}")
        for finding in comparison.findings:
            print(finding.describe())
        print(f"{comparison.unchanged} scenarios unchanged")
    return comparison.exit_code(args.perf_gate)


def load_batch(batch_dir: Path) -> Batch:
    """``batch.json`` and the ``run_index.jsonl`` rows of a batch directory.

    A run from before provenance was recorded gets its Golden Set hash from the copy of the
    tasks file every batch keeps.
    """

    meta = json.loads((batch_dir / BATCH_FILE_NAME).read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"{batch_dir / BATCH_FILE_NAME} is not a JSON object")
    provenance = dict(meta.get("provenance") or {})
    tasks_copy = batch_dir / TASKS_COPY_FILE_NAME
    if not provenance.get("tasks_sha256") and tasks_copy.is_file():
        provenance["tasks_sha256"] = tasks_sha256(tasks_copy)
    meta["provenance"] = provenance
    rows = [
        json.loads(line)
        for line in (batch_dir / RUN_INDEX_FILE_NAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return batch_dir, meta, rows


def _resolve(batches_dir: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_dir() else Path(batches_dir) / name


def _latest_pair(batches_dir: Path) -> tuple[Batch, Batch] | None:
    """The latest run and the newest earlier run of the same Golden Set."""

    batches: list[Batch] = []
    for batch_file in sorted(batches_dir.glob(f"*/{BATCH_FILE_NAME}")):
        try:
            batches.append(load_batch(batch_file.parent))
        except (OSError, ValueError):
            continue
    batches.sort(key=lambda batch: str(batch[1].get("started_at") or ""))
    if not batches:
        return None
    latest = batches[-1]
    tasks = latest[1]["provenance"].get("tasks_sha256")
    for candidate in reversed(batches[:-1]):
        if tasks and candidate[1]["provenance"].get("tasks_sha256") == tasks:
            return candidate, latest
    return None


if __name__ == "__main__":
    raise SystemExit(main())
