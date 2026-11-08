"""A scenario set as a versioned Langfuse dataset (design §5.2).

The repository is the source of truth: ``push_dataset`` upserts only the items whose
``content_sha256`` changed (each upsert is a new Langfuse version, so unchanged items are
never re-sent), archives items whose scenario left the file, and refuses a tasks file that
differs from ``HEAD`` unless allowed. Every push that wrote something, or whose content is
not journaled yet, appends a row to ``platform_datasets.jsonl``: the map from the canonical
version (``tasks_sha256`` + git commit) to Langfuse's timestamp version.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.langfuse_bridge.client import LangfuseClient
from src.agent_loop.batch import tasks_sha256
from src.agent_loop.events import read_jsonl_lines
from src.agent_loop.platform_records import (
    DatasetItemRecord,
    golden_items,
    platform_key,
)
from src.config import EvalPlatformSettings

ITEM_ID_PREFIX = "ab-"
#: Scenario sets synced as datasets: suite name -> tasks file, relative to the repository root.
SUITES = {
    "golden": "tests/golden/tasks.jsonl",
    "smoke": "tests/golden/smoke.jsonl",
    "benchmark": "tests/golden/benchmark.jsonl",
}
JOURNAL_FILE_NAME = "platform_datasets.jsonl"
JOURNAL_SCHEMA_VERSION = 1
ACTIVE, ARCHIVED = "ACTIVE", "ARCHIVED"

#: ``git`` runner: arguments in, standard output out (raises on failure).
Git = Callable[[list[str]], str]


class DatasetError(Exception):
    """A dataset push that must not run (a usage or configuration problem)."""


@dataclass(frozen=True)
class DatasetPlan:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: Unchanged here, but edited on the platform (the next push of a change overwrites it).
    edited_remotely: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True)
class DatasetPush(DatasetPlan):
    dataset: str = ""
    dataset_id: str | None = None
    tasks_sha256: str = ""
    git_commit: str | None = None
    git_dirty: bool = False
    #: Langfuse's version (the newest item ``validFrom``) after the push.
    version: str | None = None


def dataset_name(settings: EvalPlatformSettings, suite: str) -> str:
    return f"{settings.dataset_prefix}{suite}"


def item_id(dataset: str, scenario_id: str) -> str:
    """Stable across content changes (so Langfuse versions it) and unique per dataset."""

    return ITEM_ID_PREFIX + platform_key(dataset, scenario_id)


def item_body(dataset: str, record: DatasetItemRecord, status: str = ACTIVE) -> dict[str, Any]:
    return {
        "datasetName": dataset,
        "id": item_id(dataset, record.scenario_id),
        "input": record.input,
        "expectedOutput": record.expected,
        "metadata": record.metadata,
        "status": status,
    }


def plan(records: Sequence[DatasetItemRecord], remote: Sequence[Mapping[str, Any]], dataset: str) -> DatasetPlan:
    """What a push would do, from the local records and the platform's current items."""

    by_id = {str(item.get("id")): item for item in remote}
    result = DatasetPlan()
    local_ids = set()
    for record in records:
        identifier = item_id(dataset, record.scenario_id)
        local_ids.add(identifier)
        current = by_id.get(identifier)
        if current is None:
            result.added.append(record.scenario_id)
        elif (
            (current.get("metadata") or {}).get("content_sha256") != record.content_sha256
            or current.get("status") == ARCHIVED
        ):
            result.updated.append(record.scenario_id)
        else:
            result.unchanged.append(record.scenario_id)
            if current.get("input") != record.input or current.get("expectedOutput") != record.expected:
                result.edited_remotely.append(record.scenario_id)
    for identifier, current in by_id.items():
        if identifier not in local_ids and current.get("status") != ARCHIVED:
            result.archived.append(_scenario_of(current))
    return result


def push_dataset(
    client: LangfuseClient | None,
    settings: EvalPlatformSettings,
    *,
    suite: str,
    tasks_path: Path,
    source_path: str,
    journal_path: Path,
    git: Git,
    allow_dirty: bool = False,
    dry_run: bool = False,
) -> DatasetPush:
    """Bring the platform's dataset of ``suite`` to the content of ``tasks_path``.

    ``dry_run`` reads only the file (no network, no journal): every scenario is listed as
    added, since the platform's state is not read.
    """

    name = dataset_name(settings, suite)
    records = golden_items(tasks_path, dataset=name, suite=suite, source_path=source_path)
    content_hash = tasks_sha256(tasks_path)
    try:
        dirty = bool(git(["status", "--porcelain", "--", source_path]).strip())
        commit = git(["log", "-1", "--format=%H", "--", source_path]).strip() or None
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DatasetError(f"git could not tell the state of {source_path} ({type(exc).__name__})") from None
    if dirty and not allow_dirty and not dry_run:
        raise DatasetError(
            f"{source_path} differs from the committed version; commit it or pass --allow-dirty"
        )
    if dry_run or client is None:
        return DatasetPush(
            added=[record.scenario_id for record in records],
            dry_run=True,
            dataset=name,
            tasks_sha256=content_hash,
            git_commit=commit,
            git_dirty=dirty,
        )

    dataset_id = client.ensure_dataset(
        name, {"source_path": source_path, "tasks_sha256": content_hash, "git_commit": commit}
    )
    remote = client.dataset_items(name)
    steps = plan(records, remote, name)
    by_scenario = {record.scenario_id: record for record in records}
    versions: list[str] = []
    for scenario_id in [*steps.added, *steps.updated]:
        versions.append(_version_of(client.upsert_dataset_item(item_body(name, by_scenario[scenario_id]))))
    remote_by_scenario = {_scenario_of(item): item for item in remote}
    for scenario_id in steps.archived:
        current = remote_by_scenario[scenario_id]
        body = {
            "datasetName": name,
            "id": current["id"],
            "input": current.get("input"),
            "expectedOutput": current.get("expectedOutput"),
            "metadata": current.get("metadata"),
            "status": ARCHIVED,
        }
        versions.append(_version_of(client.upsert_dataset_item(body)))
    if not versions:
        versions = [_version_of(item) for item in remote]
    version = max((value for value in versions if value), default=None)

    result = DatasetPush(
        added=steps.added,
        updated=steps.updated,
        archived=steps.archived,
        unchanged=steps.unchanged,
        edited_remotely=steps.edited_remotely,
        dataset=name,
        dataset_id=dataset_id,
        tasks_sha256=content_hash,
        git_commit=commit,
        git_dirty=dirty,
        version=version,
    )
    wrote = bool(steps.added or steps.updated or steps.archived)
    project_id = client.project().id
    if wrote or not _journaled(journal_path, client.host, project_id, name, content_hash):
        _append_journal(journal_path, client.host, project_id, result)
    return result


def dataset_status(
    client: LangfuseClient,
    settings: EvalPlatformSettings,
    *,
    suite: str,
    tasks_path: Path,
    source_path: str,
) -> DatasetPlan:
    """What ``push_dataset`` would change now, and items edited on the platform (read only)."""

    name = dataset_name(settings, suite)
    records = golden_items(tasks_path, dataset=name, suite=suite, source_path=source_path)
    return plan(records, client.dataset_items(name), name)


def load_journal(path: Path) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    rows = []
    for line in read_jsonl_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line of an interrupted append
        if isinstance(row, dict):
            rows.append(row)
    return rows


def journaled_version(
    path: Path, *, host: str, project_id: str, dataset: str, content_hash: str
) -> dict[str, Any] | None:
    """The newest journal row of this content on this project, ``None`` when never pushed."""

    rows = [
        row
        for row in load_journal(path)
        if (row.get("host"), row.get("project_id"), row.get("dataset"), row.get("tasks_sha256"))
        == (host, project_id, dataset, content_hash)
    ]
    return rows[-1] if rows else None


def run_git(repo_root: Path) -> Git:
    """A ``git`` runner in ``repo_root``; output decoded as UTF-8."""

    def run(args: list[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout

    return run


def _journaled(path: Path, host: str, project_id: str, dataset: str, content_hash: str) -> bool:
    return journaled_version(path, host=host, project_id=project_id, dataset=dataset, content_hash=content_hash) is not None


def _append_journal(path: Path, host: str, project_id: str, result: DatasetPush) -> None:
    row = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "pushed_at": datetime.now(UTC).isoformat(),
        "host": host,
        "project_id": project_id,
        "dataset": result.dataset,
        "dataset_id": result.dataset_id,
        "tasks_sha256": result.tasks_sha256,
        "git_commit": result.git_commit,
        "git_dirty": result.git_dirty,
        "version": result.version,
        "added": len(result.added),
        "updated": len(result.updated),
        "archived": len(result.archived),
        "unchanged": len(result.unchanged),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    prefix = "" if not text or text.endswith("\n") else "\n"  # after a torn last line
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(prefix + json.dumps(row, ensure_ascii=False) + "\n")


def _version_of(item: Mapping[str, Any]) -> str:
    return str(item.get("version") or item.get("updatedAt") or "")


def _scenario_of(item: Mapping[str, Any]) -> str:
    return str((item.get("metadata") or {}).get("scenario_id") or item.get("id"))


def suite_of(tasks_path: Path, root: Path) -> str | None:
    """The suite whose tasks file is ``tasks_path``; ``None`` for any other file.

    A batch of another file must not be synced as a suite: ``push_dataset`` would archive every
    item of that suite's dataset the file lacks.
    """

    resolved = Path(tasks_path).resolve()
    return next((name for name, path in SUITES.items() if (root / path).resolve() == resolved), None)
