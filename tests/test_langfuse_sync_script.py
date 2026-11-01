"""``scripts/langfuse_sync.py``: the CLI in process (``main(argv)``) and its exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from scripts import langfuse_sync
from scripts.langfuse_bridge.client import LangfuseClient
from src.config import EvalPlatformSettings, Settings, StorageSettings
from tests.platform_fixtures.fake_langfuse import PUBLIC_KEY, SECRET_KEY, FakeLangfuse
from tests.test_langfuse_datasets import SCENARIOS, Git, write


def make_settings(tmp_path: Path, **platform: Any) -> Settings:
    values: dict[str, Any] = {
        "enabled": True,
        "host": "https://platform.example.test",
        "public_key": SecretStr(PUBLIC_KEY),
        "secret_key": SecretStr(SECRET_KEY),
        "requests_per_minute": 1000,
    }
    values.update(platform)
    return Settings.model_construct(
        eval_platform=EvalPlatformSettings(**values),
        storage=StorageSettings(root_dir=tmp_path / "home"),
    )


def run(
    argv: list[str],
    tmp_path: Path,
    fake: FakeLangfuse | None,
    *,
    git: Git | None = None,
    **platform: Any,
) -> int:
    settings = make_settings(tmp_path, **platform)

    def factory(platform_settings: EvalPlatformSettings) -> LangfuseClient:
        if fake is None:
            raise AssertionError("no network expected")
        return LangfuseClient(platform_settings, transport=fake.transport(), sleep=lambda _s: None)

    return langfuse_sync.main(argv, settings=settings, client_factory=factory, git=git or Git())


def test_dataset_push_syncs_and_prints_a_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    code = run(["dataset", "push", "--suite", "golden", "--tasks", str(tasks)], tmp_path, fake)

    out = capsys.readouterr().out
    assert code == 0
    assert "dataset autobrowser/golden: 2 added · 0 updated · 0 archived · 0 unchanged" in out
    assert len(fake.items) == 2
    journal = tmp_path / "home" / "batches" / "platform_datasets.jsonl"
    assert json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["added"] == 2


def test_dataset_push_dry_run_touches_no_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    code = run(["dataset", "push", "--tasks", str(tasks), "--dry-run"], tmp_path, None, enabled=False, public_key=None)

    assert code == 0
    assert "dry run" in capsys.readouterr().out


def test_dataset_status_lists_changes_and_ui_edits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    run(["dataset", "push", "--tasks", str(tasks)], tmp_path, fake)
    write(tasks, SCENARIOS[:1])

    code = run(["dataset", "status", "--tasks", str(tasks)], tmp_path, fake)

    out = capsys.readouterr().out
    assert code == 0
    assert "archived: quotes-1" in out


@pytest.mark.parametrize(
    "argv, platform, fake_kwargs, git, message",
    [
        (["dataset", "push"], {"enabled": False}, {}, None, "eval_platform.enabled"),
        (["dataset", "push"], {"secret_key": None}, {}, None, "secret_key"),
        (["dataset", "push"], {}, {"version": "3.120.0"}, None, "4.59.0"),
        (["dataset", "push"], {}, {}, Git(dirty=True), "--allow-dirty"),
    ],
)
def test_configuration_and_usage_problems_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    platform: dict[str, Any],
    fake_kwargs: dict[str, Any],
    git: Git | None,
    message: str,
) -> None:
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)

    code = run([*argv, "--tasks", str(tasks)], tmp_path, FakeLangfuse(**fake_kwargs), git=git, **platform)

    assert code == 2
    assert message in capsys.readouterr().err


def test_bad_keys_exit_2_and_an_outage_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tasks = write(tmp_path / "tasks.jsonl", SCENARIOS)
    unauthorized = FakeLangfuse()
    unauthorized.fail("GET", "/projects", status=401)
    down = FakeLangfuse()
    down.fail("GET", "/health", status=503, times=10)

    assert run(["dataset", "push", "--tasks", str(tasks)], tmp_path, unauthorized) == 2
    assert run(["dataset", "push", "--tasks", str(tasks)], tmp_path, down, max_retries=0) == 1
    assert SECRET_KEY not in capsys.readouterr().err


# -- experiment push and status ---------------------------------------------------------------


from tests.platform_fixtures.batch_folder import Scenario, make_batch  # noqa: E402


def batches(tmp_path: Path) -> Path:
    return tmp_path / "home" / "batches"


def test_experiment_push_syncs_one_batch_by_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    make_batch(batches(tmp_path), [Scenario("books-1"), Scenario("quotes-1")])

    code = run(["experiment", "push", "--batch", "batch-1a2b3c4d5e6f"], tmp_path, fake)

    out = capsys.readouterr().out
    assert code == 0
    assert "langfuse: batch-1a2b3c4d5e6f complete · 2 exported" in out
    assert len({span["traceId"] for span in fake.spans()}) == 2


def test_experiment_push_dry_run_needs_no_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    batch = make_batch(batches(tmp_path), [Scenario("books-1")])

    code = run(["experiment", "push", "--batch", str(batch), "--dry-run"], tmp_path, None)

    assert code == 0 and "skipped" in capsys.readouterr().out


def test_experiment_push_pending_catches_up_every_unfinished_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    first = make_batch(batches(tmp_path), [Scenario("a")], batch_id="batch-aaaaaaaaaaaa")
    make_batch(batches(tmp_path), [Scenario("b")], batch_id="batch-bbbbbbbbbbbb")
    fake.fail("GET", "/health", status=503, times=10)
    assert run(["experiment", "push", "--batch", str(first)], tmp_path, fake, max_retries=0) == 1
    fake.faults.clear()

    code = run(["experiment", "push", "--pending"], tmp_path, fake)

    out = capsys.readouterr().out
    assert code == 0
    assert "batch-aaaaaaaaaaaa complete" in out and "batch-bbbbbbbbbbbb" not in out


def test_experiment_push_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    make_batch(batches(tmp_path), [Scenario("a")], batch_id="batch-running", finished=False)

    assert run(["experiment", "push", "--batch", "batch-running"], tmp_path, fake) == 2
    assert run(["experiment", "push", "--batch", "missing"], tmp_path, fake) == 2
    assert run(["experiment", "push"], tmp_path, fake) == 2
    assert "not finished" in capsys.readouterr().err


def test_status_lists_ledgers_and_verify_finds_duplicated_observations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLangfuse()
    make_batch(batches(tmp_path), [Scenario("a"), Scenario("b")])
    run(["experiment", "push", "--batch", "batch-1a2b3c4d5e6f"], tmp_path, fake)
    capsys.readouterr()

    assert run(["status"], tmp_path, fake) == 0
    assert "batch-1a2b3c4d5e6f complete · 2/2 exported" in capsys.readouterr().out

    fake.traces.append(fake.traces[0])  # a re-sent trace: every observation twice
    assert run(["status", "--batch", "batch-1a2b3c4d5e6f", "--verify"], tmp_path, fake) == 1
    assert "duplicated: a" in capsys.readouterr().out
