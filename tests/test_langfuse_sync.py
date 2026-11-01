"""``scripts/langfuse_bridge/sync.py`` and ``ledger.py``: idempotent, resumable batch sync."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from scripts.langfuse_bridge.client import LangfuseClient
from scripts.langfuse_bridge.datasets import item_id
from scripts.langfuse_bridge.ledger import LEDGER_FILE_NAME, LOCK_FILE_NAME, LedgerLocked, SyncLedger, ledger_lock
from scripts.langfuse_bridge.sync import SyncReport, SyncUsageError, push_batch
from src.config import EvalPlatformSettings, Settings, StorageSettings
from tests.platform_fixtures.batch_folder import Scenario, make_batch
from tests.platform_fixtures.fake_langfuse import PUBLIC_KEY, SECRET_KEY, FakeLangfuse
from tests.test_langfuse_datasets import Git

NOW = datetime(2026, 11, 1, 15, 0, 0, tzinfo=UTC)


def make_settings(tmp_path: Path, **platform: Any) -> Settings:
    values: dict[str, Any] = {
        "enabled": True,
        "host": "https://platform.example.test",
        "public_key": SecretStr(PUBLIC_KEY),
        "secret_key": SecretStr(SECRET_KEY),
        "requests_per_minute": 1000,
        "max_retries": 0,
    }
    values.update(platform)
    return Settings.model_construct(
        eval_platform=EvalPlatformSettings(**values),
        storage=StorageSettings(root_dir=tmp_path / "home"),
    )


class Env:
    def __init__(self, tmp_path: Path, fake: FakeLangfuse | None = None) -> None:
        self.tmp_path = tmp_path
        self.fake = fake or FakeLangfuse()
        self.batches = tmp_path / "home" / "batches"
        self.now = NOW
        self.clock = 0.0
        self.repo_tasks = tmp_path / "repo-tasks.jsonl"
        self.repo_tasks.write_text('{"scenario_id": "unrelated", "tasks": ["x"]}\n', encoding="utf-8")

    def batch(self, scenarios: list[Scenario], **kwargs: Any) -> Path:
        return make_batch(self.batches, scenarios, **kwargs)

    def push(self, batch_dir: Path, **kwargs: Any) -> SyncReport:
        platform = kwargs.pop("platform", {})
        settings = make_settings(self.tmp_path, **platform)

        def factory(platform_settings: EvalPlatformSettings) -> LangfuseClient:
            return LangfuseClient(platform_settings, transport=self.fake.transport(), sleep=lambda _s: None)

        kwargs.setdefault("repo_tasks", self.repo_tasks)
        return push_batch(
            batch_dir,
            settings,
            client_factory=factory,
            git=Git(),
            clock=lambda: self.clock,
            now=lambda: self.now,
            **kwargs,
        )

    def ledger(self, batch_dir: Path) -> dict[str, Any]:
        return json.loads((batch_dir / LEDGER_FILE_NAME).read_text(encoding="utf-8"))

    def trace_ids(self) -> list[str]:
        return sorted({span["traceId"] for span in self.fake.spans()})


SCENARIOS = [Scenario("books-1"), Scenario("quotes-1"), Scenario("books-2", failure_kind="grade_failed", grade="failed")]


# -- first push and idempotency ------------------------------------------------------------


def test_a_first_push_exports_every_scenario_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)

    report = env.push(batch)

    assert report.state == "complete" and report.exit_code() == 0
    assert (report.exported, report.unknown, report.pending) == (3, 0, 0)
    assert len(env.trace_ids()) == 3
    ledger = env.ledger(batch)
    assert ledger["schema_version"] == 1 and ledger["platform"] == "langfuse"
    assert ledger["project_id"] == "project-test"
    assert ledger["experiment"]["id"] == "batch-1a2b3c4d5e6f" and ledger["experiment"]["attempt"] == 1
    assert ledger["experiment"]["url"] == report.experiment_url and report.experiment_url.startswith("https://platform.example.test/project/project-test/")
    assert ledger["dataset"]["name"] == "autobrowser/golden"
    assert {name: item["state"] for name, item in ledger["items"].items()} == {"books-1": "exported", "quotes-1": "exported", "books-2": "exported"}
    assert ledger["state"] == "complete" and ledger["last_error"] is None


def test_a_repeated_push_sends_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.push(batch)
    requests = len(env.fake.requests)

    report = env.push(batch)

    assert report.state == "complete"
    assert len(env.fake.requests) == requests


def test_the_ledger_holds_no_secret(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.fake.fail("POST", "/otel/v1/traces", status=401)

    with pytest.raises(SyncUsageError, match="public_key"):
        env.push(batch)

    text = (batch / LEDGER_FILE_NAME).read_text(encoding="utf-8")
    assert SECRET_KEY not in text and PUBLIC_KEY not in text


# -- failures and recovery -----------------------------------------------------------------


def test_an_outage_mid_list_leaves_the_rest_pending_and_a_rerun_completes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.fake.fail("POST", "/otel/v1/traces", status=503, times=5, skip=1)

    first = env.push(batch)

    assert first.state == "partial" and first.exit_code() == 1
    assert (first.exported, first.pending) == (1, 2)
    ledger = env.ledger(batch)
    assert ledger["state"] == "pending" and "503" in ledger["last_error"]
    env.fake.faults.clear()

    second = env.push(batch)

    assert second.state == "complete" and second.exported == 3
    assert len(env.trace_ids()) == 3 and len(env.fake.traces) == 3
    span_ids = [span["spanId"] for span in env.fake.spans()]
    assert len(span_ids) == len(set(span_ids))


def test_an_unreachable_platform_leaves_the_batch_pending(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.fake.fail("GET", "/health", exception=httpx.ConnectError, times=5)

    report = env.push(batch)

    assert report.state == "pending" and report.exit_code() == 1
    assert "ConnectError" in (report.error or "")
    assert env.ledger(batch)["state"] == "pending"


def test_an_uncertain_export_is_verified_before_it_is_sent_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS[:1])
    env.fake.fail("POST", "/otel/v1/traces", exception=httpx.ReadTimeout, after_effect=True)

    first = env.push(batch)
    assert first.unknown == 1 and env.ledger(batch)["items"]["books-1"]["state"] == "unknown"

    env.now = NOW + timedelta(seconds=10)
    early = env.push(batch)
    assert early.unknown == 1 and len(env.fake.traces) == 1

    env.now = NOW + timedelta(seconds=120)
    late = env.push(batch)
    assert late.state == "complete" and late.exported == 1
    assert len(env.fake.traces) == 1  # found, never re-sent


def test_an_uncertain_export_that_never_arrived_is_sent_again_after_the_grace(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS[:1])
    env.fake.fail("POST", "/otel/v1/traces", exception=httpx.ReadTimeout)

    env.push(batch)
    env.now = NOW + timedelta(seconds=120)
    report = env.push(batch)

    assert report.state == "complete" and len(env.fake.traces) == 1


def test_a_split_trace_resumes_after_its_last_sent_part(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch([Scenario("books-1", tool_calls=40)])
    env.fake.fail("POST", "/otel/v1/traces", status=503, times=3, skip=1)
    platform = {"max_request_bytes": 20_000}

    env.push(batch, platform=platform)
    item = env.ledger(batch)["items"]["books-1"]
    assert item["state"] == "pending" and item["parts_sent"] >= 1 and item["parts"] > item["parts_sent"]
    env.fake.faults.clear()
    env.push(batch, platform=platform)

    ids = [span["spanId"] for span in env.fake.spans()]
    assert len(ids) == len(set(ids)) == env.ledger(batch)["items"]["books-1"]["spans"]


def test_the_budget_stops_between_items(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)

    report = env.push(batch, deadline=-1.0)

    assert report.state == "partial" and report.pending == 3 and env.fake.traces == []


# -- lock, project, attempts -------------------------------------------------------------------


def test_a_held_lock_refuses_and_a_stale_one_is_taken_over(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS[:1])
    (batch / LOCK_FILE_NAME).write_text(json.dumps({"pid": 1, "at": NOW.isoformat()}), encoding="utf-8")

    with pytest.raises(LedgerLocked):
        env.push(batch)

    env.now = NOW + timedelta(seconds=601)  # 2 x sync_timeout_seconds (300)
    assert env.push(batch).state == "complete"
    assert not (batch / LOCK_FILE_NAME).exists()


def test_ledger_lock_is_exclusive(tmp_path: Path) -> None:
    with ledger_lock(tmp_path, stale_after_seconds=600, now=lambda: NOW):
        with pytest.raises(LedgerLocked), ledger_lock(tmp_path, stale_after_seconds=600, now=lambda: NOW):
            pass
    assert not (tmp_path / LOCK_FILE_NAME).exists()


def test_another_project_needs_a_new_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS[:2])
    env.fake.fail("POST", "/otel/v1/traces", status=503, skip=1)
    assert env.push(batch).state == "partial"
    first_traces = env.trace_ids()
    env.fake.project_id = "project-other"

    with pytest.raises(SyncUsageError, match="--new-attempt"):
        env.push(batch)

    report = env.push(batch, new_attempt=True)

    ledger = env.ledger(batch)
    assert report.state == "complete"
    assert ledger["experiment"]["id"] == "batch-1a2b3c4d5e6f.2" and ledger["experiment"]["attempt"] == 2
    assert ledger["project_id"] == "project-other"
    assert len(set(env.trace_ids()) - set(first_traces)) == 2


def test_a_complete_batch_is_not_synced_again_without_a_new_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS[:1])
    env.push(batch)
    env.fake.project_id = "project-other"
    requests = len(env.fake.requests)

    assert env.push(batch).state == "complete" and len(env.fake.requests) == requests
    assert env.push(batch, new_attempt=True).state == "complete"
    assert env.ledger(batch)["experiment"]["id"].endswith(".2")


def test_an_old_or_unfinished_batch_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    old = env.batch(SCENARIOS[:1], batch_id="batch-old")
    data = json.loads((old / "batch.json").read_text(encoding="utf-8"))
    del data["schema_version"]
    (old / "batch.json").write_text(json.dumps(data), encoding="utf-8")
    running = env.batch(SCENARIOS[:1], batch_id="batch-running", finished=False)

    with pytest.raises(SyncUsageError, match="schema"):
        env.push(old)
    with pytest.raises(SyncUsageError, match="finished"):
        env.push(running)


def test_a_dry_run_touches_no_network_and_no_ledger(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)

    report = env.push(batch, dry_run=True)

    assert report.state == "skipped" and report.pending == 3
    assert env.fake.requests == [] and not (batch / LEDGER_FILE_NAME).exists()


# -- dataset version ------------------------------------------------------------------------------


def test_a_batch_on_the_current_set_pushes_the_dataset_and_records_its_version(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.repo_tasks.write_bytes((batch / "tasks.jsonl").read_bytes())

    env.push(batch)

    dataset = env.ledger(batch)["dataset"]
    assert dataset["version_matched"] is True and dataset["version"]
    assert len(env.fake.items) == 3
    root = next(span for span in env.fake.spans() if span["name"] == "scenario books-1")
    keys = {item["key"]: item["value"] for item in root["attributes"]}
    assert keys["langfuse.experiment.item.version"] == {"stringValue": dataset["version"]}


def test_a_batch_on_an_old_set_is_unmatched_and_missing_items_are_archived(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)

    env.push(batch)

    dataset = env.ledger(batch)["dataset"]
    assert dataset["version_matched"] is False
    assert {item["status"] for item in env.fake.items.values()} == {"ARCHIVED"}
    assert set(env.fake.items) == {item_id("autobrowser/golden", s.scenario_id) for s in SCENARIOS}
    root = next(span for span in env.fake.spans() if span["name"] == "scenario books-1")
    keys = {item["key"] for item in root["attributes"]}
    assert "langfuse.experiment.item.version" not in keys


def test_ledger_writes_are_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = SyncLedger.load(tmp_path)
    ledger.data["state"] = "pending"
    calls: list[tuple[str, str]] = []
    real = os.replace

    def flaky(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        if len(calls) == 1:
            raise PermissionError("antivirus")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    ledger.save(retry_after_seconds=0.0)

    assert len(calls) == 2
    assert json.loads((tmp_path / LEDGER_FILE_NAME).read_text(encoding="utf-8"))["state"] == "pending"
    assert [path.name for path in tmp_path.iterdir()] == [LEDGER_FILE_NAME]
