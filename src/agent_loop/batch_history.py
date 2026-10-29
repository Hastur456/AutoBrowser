"""The run history of batch runs: one small row per scenario per run.

``<storage.batches_dir>/history.jsonl`` answers "how did this scenario do over the last runs"
without opening every batch folder; ``scripts/compare_batches.py`` and DuckDB read it. A row
holds the run's identity (commit, model, Golden Set and settings fingerprints), the scenario's
grade and failure kind and its metrics — never answers or page text. The file keeps the newest
``pipeline.history_max_rows`` rows.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.agent_loop.batch_report import SUMMED_METRICS

HISTORY_FILE_NAME = "history.jsonl"
#: Version of the history row format.
HISTORY_SCHEMA_VERSION = 1


def history_rows(
    metadata: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The history rows of one run, from its ``batch.json`` metadata and ``run_index`` rows."""

    provenance = metadata.get("provenance") or {}
    git = provenance.get("git") or {}
    identity = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "batch_id": metadata.get("batch_id"),
        "started_at": metadata.get("started_at"),
        "git": {"commit": git.get("commit"), "dirty": git.get("dirty")},
        "model": provenance.get("model"),
        "tasks_sha256": provenance.get("tasks_sha256"),
        "settings_sha256": provenance.get("settings_sha256"),
    }
    history: list[dict[str, Any]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        history.append(
            {
                **identity,
                "scenario_id": row.get("scenario_id"),
                "grade_status": (row.get("grade") or {}).get("status"),
                "failure_kind": row.get("failure_kind"),
                **{key: metrics.get(key) for key in SUMMED_METRICS},
            }
        )
    return history


def append_history(path: Path, rows: Iterable[Mapping[str, Any]], max_rows: int) -> None:
    """Append ``rows``; past ``max_rows`` lines, keep only the newest ones."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) <= max_rows:
        return
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text("\n".join(lines[-max_rows:]) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_history(path: Path) -> list[dict[str, Any]]:
    """Every history row, oldest first; a missing file is an empty history."""

    path = Path(path)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


__all__ = [
    "HISTORY_FILE_NAME",
    "HISTORY_SCHEMA_VERSION",
    "append_history",
    "history_rows",
    "load_history",
]
