"""``scripts/langfuse_bridge/datasets.py``: a scenario set as a versioned Langfuse dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.langfuse_bridge.datasets import (
    DatasetError,
    dataset_name,
    dataset_status,
    item_id,
    load_journal,
    push_dataset,
)
from src.agent_loop.batch import tasks_sha256
from tests.platform_fixtures.fake_langfuse import FakeLangfuse
from tests.test_langfuse_client import client, settings

SCENARIOS = [
    {"scenario_id": "books-1", "tasks": ["find the price"], "expected": {"contains": "£"}, "tags": ["books"]},
    {"scenario_id": "quotes-1", "tasks": ["find a quote"], "expected": {"fields": ["text"]}, "reference": "One real quote."},
]


class Git:
    """A fake ``git`` for one tasks file: its last commit and whether it is dirty."""

    def __init__(self, commit: str = "c" * 40, dirty: bool = False) -> None:
        self.commit = commit
        self.dirty = dirty
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "status":
            return " M tests/golden/tasks.jsonl\n" if self.dirty else ""
        if args[0] == "log":
            return self.commit + "\n"
        raise AssertionError(args)


def write(path: Path, scenarios: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in scenarios) + "\n", encoding="utf-8")
    return path


def push(fake: FakeLangfuse, tasks: Path, tmp_path: Path, git: Git | None = None, **kwargs: Any) -> Any:
    return push_dataset(
        client(fake),
        settings(),
        suite="golden",
        tasks_path=tasks,
        source_path="tests/golden/tasks.jsonl",
        journal_path=tmp_path / "platform_datasets.jsonl",
        git=git or Git(),
        **kwargs,
    )


def test_item_ids_are_stable_prefixed_and_differ_between_datasets() -> None:
    assert item_id("autobrowser/golden", "books-1") == item_id("autobrowser/golden", "books-1")
    assert item_id("autobrowser/golden", "books-1").startswith("ab-")
    assert len(item_id("autobrowser/golden", "books-1")) == 35
    assert item_id("autobrowser/golden", "books-1") != item_id("autobrowser/live-local", "books-1")
    assert dataset_name(settings(), "golden") == "autobrowser/golden"


def test_a_first_push_creates_the_dataset_and_every_item(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    result = push(fake, tasks, tmp_path)

    assert (result.added, result.updated, result.archived, result.unchanged) == (["books-1", "quotes-1"], [], [], [])
    dataset = fake.datasets["autobrowser/golden"]
    assert dataset["metadata"] == {"source_path": "tests/golden/tasks.jsonl", "tasks_sha256": tasks_sha256(tasks), "git_commit": "c" * 40}
    item = fake.items[item_id("autobrowser/golden", "quotes-1")]
    assert item["input"] == {"tasks": ["find a quote"], "scenario_id": "quotes-1"}
    assert item["expectedOutput"] == {"checks": {"fields": ["text"]}, "reference": "One real quote."}
    assert item["metadata"]["source_path"] == "tests/golden/tasks.jsonl"
    assert item["status"] == "ACTIVE"
    assert result.version == max(entry["version"] for entry in fake.items.values())


def test_a_push_without_changes_writes_nothing(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    push(fake, tasks, tmp_path)
    writes = len(fake.writes())

    result = push(fake, tasks, tmp_path)

    assert len(fake.writes()) == writes
    assert result.unchanged == ["books-1", "quotes-1"] and not result.added and not result.updated
    assert len(load_journal(tmp_path / "platform_datasets.jsonl")) == 1


def test_a_changed_scenario_is_updated_and_a_removed_one_archived(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    push(fake, tasks, tmp_path)
    changed = [{**SCENARIOS[0], "expected": {"contains": "$"}}, {"scenario_id": "new-1", "tasks": ["x"]}]

    result = push(fake, write(tasks, changed), tmp_path, Git(commit="d" * 40))

    assert (result.added, result.updated, result.archived, result.unchanged) == (["new-1"], ["books-1"], ["quotes-1"], [])
    books = fake.items[item_id("autobrowser/golden", "books-1")]
    assert books["expectedOutput"]["checks"] == {"contains": "$"}
    assert len(fake.item_versions[item_id("autobrowser/golden", "books-1")]) == 2
    archived = fake.items[item_id("autobrowser/golden", "quotes-1")]
    assert archived["status"] == "ARCHIVED" and archived["input"]["scenario_id"] == "quotes-1"


def test_a_returning_scenario_is_reactivated_under_the_same_id(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    push(fake, tasks, tmp_path)
    push(fake, write(tasks, SCENARIOS[:1]), tmp_path, Git(commit="d" * 40))

    result = push(fake, write(tasks, SCENARIOS), tmp_path, Git(commit="e" * 40))

    assert result.updated == ["quotes-1"]
    assert fake.items[item_id("autobrowser/golden", "quotes-1")]["status"] == "ACTIVE"


def test_a_dirty_tasks_file_is_refused_unless_allowed(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    with pytest.raises(DatasetError, match="--allow-dirty"):
        push(fake, tasks, tmp_path, Git(dirty=True))
    assert fake.writes() == []

    push(fake, tasks, tmp_path, Git(dirty=True), allow_dirty=True)
    row = load_journal(tmp_path / "platform_datasets.jsonl")[-1]
    assert row["git_dirty"] is True


def test_every_push_with_new_content_is_journaled(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    push(fake, tasks, tmp_path)
    first_hash = tasks_sha256(tasks)
    push(fake, write(tasks, SCENARIOS[:1]), tmp_path, Git(commit="d" * 40))

    rows = load_journal(tmp_path / "platform_datasets.jsonl")

    assert [row["tasks_sha256"] for row in rows] == [first_hash, tasks_sha256(tasks)]
    first, second = rows
    assert first["schema_version"] == 1
    assert (first["host"], first["project_id"], first["dataset"]) == ("https://platform.example.test", "project-test", "autobrowser/golden")
    assert (first["added"], first["updated"], first["archived"], first["unchanged"]) == (2, 0, 0, 0)
    assert (second["git_commit"], second["archived"], second["unchanged"]) == ("d" * 40, 1, 1)
    assert first["version"] and first["dataset_id"] == fake.datasets["autobrowser/golden"]["id"]
    assert "secret" not in json.dumps(rows).lower()


def test_a_dry_run_reads_only_the_file(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    result = push_dataset(
        None,
        settings(),
        suite="golden",
        tasks_path=tasks,
        source_path="tests/golden/tasks.jsonl",
        journal_path=tmp_path / "platform_datasets.jsonl",
        git=Git(),
        dry_run=True,
    )

    assert result.dry_run and result.unchanged == [] and result.added == ["books-1", "quotes-1"]
    assert fake.requests == []
    assert not (tmp_path / "platform_datasets.jsonl").exists()


def test_status_shows_pending_changes_and_edits_made_in_the_ui(tmp_path: Path) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    push(fake, tasks, tmp_path)
    fake.items[item_id("autobrowser/golden", "books-1")]["expectedOutput"] = {"checks": {"contains": "edited"}}
    write(tasks, [SCENARIOS[0], {**SCENARIOS[1], "tasks": ["find two quotes"]}, {"scenario_id": "n", "tasks": ["x"]}])
    writes = len(fake.writes())

    status = dataset_status(client(fake), settings(), suite="golden", tasks_path=tasks, source_path="tests/golden/tasks.jsonl")

    assert (status.added, status.updated, status.archived, status.unchanged) == (["n"], ["quotes-1"], [], ["books-1"])
    assert status.edited_remotely == ["books-1"]
    assert len(fake.writes()) == writes
