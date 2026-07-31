from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import run_batch as run_batch_script


@dataclass(frozen=True)
class FakeConfig:
    model: str
    no_mcp: bool
    recursion_limit: int
    chrome_path: str
    user_data_dir: str
    cdp_port: int


class FakeSession:
    def __init__(self, config: FakeConfig) -> None:
        self.config = config


@pytest.mark.asyncio
async def test_run_batch_from_args_builds_session_and_passes_batch_options(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text('{"scenario_id":"s1","task":"inspect"}\n', encoding="utf-8")
    parser = run_batch_script.build_parser()
    args = parser.parse_args(
        [
            "--tasks",
            str(tasks_path),
            "--continue-on-error",
            "--model",
            "test-model",
            "--no-mcp",
            "--recursion-limit",
            "7",
            "--chrome-path",
            "chrome-test.exe",
            "--user-data-dir",
            "profile-test",
            "--cdp-port",
            "9555",
        ]
    )
    calls: dict[str, Any] = {}

    def session_builder(received: argparse.Namespace) -> FakeSession:
        calls["session_args"] = received
        return FakeSession(
            FakeConfig(
                model=received.model,
                no_mcp=received.no_mcp,
                recursion_limit=received.recursion_limit,
                chrome_path=received.chrome_path,
                user_data_dir=received.user_data_dir,
                cdp_port=received.cdp_port,
            )
        )

    async def batch_runner(**kwargs: Any) -> dict[str, Any]:
        calls["batch_kwargs"] = kwargs
        return {"batch_id": "batch-1", "status": "completed"}

    result = await run_batch_script.run_batch_from_args(
        args,
        session_builder=session_builder,  # type: ignore[arg-type]
        batch_runner=batch_runner,
    )

    assert result == {"batch_id": "batch-1", "status": "completed"}
    assert calls["session_args"] is args
    assert calls["batch_kwargs"]["tasks_path"] == tasks_path
    assert calls["batch_kwargs"]["session"].config.model == "test-model"
    assert calls["batch_kwargs"]["continue_on_error"] is True
    assert calls["batch_kwargs"]["config"] == {
        "model": "test-model",
        "no_mcp": True,
        "recursion_limit": 7,
        "chrome_path": "chrome-test.exe",
        "user_data_dir": "profile-test",
        "cdp_port": 9555,
    }
    assert args.temperature == 0.0
    assert args.show_state is False
    assert args.hide_snapshot is False
    assert args.show_tools is False
    assert args.json is False
    assert args.compress_tools is False
    assert args.cdp_timeout == 30.0


def test_main_prints_batch_result_without_real_runtime(monkeypatch, tmp_path: Path, capsys) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text('{"scenario_id":"s1","task":"inspect"}\n', encoding="utf-8")

    async def fake_run_batch_from_args(args: argparse.Namespace) -> dict[str, Any]:
        return {
            "batch_id": "batch-1",
            "tasks_path": str(args.tasks),
            "continue_on_error": args.continue_on_error,
        }

    monkeypatch.setattr(run_batch_script, "run_batch_from_args", fake_run_batch_from_args)

    exit_code = run_batch_script.main(
        [
            "--tasks",
            str(tasks_path),
            "--continue-on-error",
            "--no-mcp",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "batch_id": "batch-1",
        "tasks_path": str(tasks_path),
        "continue_on_error": True,
    }
