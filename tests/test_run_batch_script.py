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
async def test_run_batch_from_args_passes_session_factory_and_batch_options(
    tmp_path: Path,
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text('{"scenario_id":"s1","tasks":["inspect"]}\n', encoding="utf-8")
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
    calls: dict[str, Any] = {"session_args": []}

    def session_builder(received: argparse.Namespace) -> FakeSession:
        calls["session_args"].append(received)
        return _fake_session_from_args(received)

    async def batch_runner(**kwargs: Any) -> dict[str, Any]:
        calls["batch_kwargs"] = kwargs
        session = kwargs["session_factory"]()
        return {
            "batch_id": "batch-1",
            "status": "completed",
            "session_model": session.config.model,
        }

    result = await run_batch_script.run_batch_from_args(
        args,
        session_builder=session_builder,  # type: ignore[arg-type]
        batch_runner=batch_runner,
    )

    assert result == {
        "batch_id": "batch-1",
        "status": "completed",
        "session_model": "test-model",
    }
    assert calls["batch_kwargs"]["tasks_path"] == tasks_path
    assert "session" not in calls["batch_kwargs"]
    assert callable(calls["batch_kwargs"]["session_factory"])
    assert calls["batch_kwargs"]["continue_on_error"] is True
    assert calls["batch_kwargs"]["config"] == {
        "model": "test-model",
        "no_mcp": True,
        "recursion_limit": 7,
        "chrome_path": "chrome-test.exe",
        "user_data_dir": "profile-test",
        "cdp_port": 9555,
    }
    assert len(calls["session_args"]) == 1
    assert calls["session_args"][0] is not args
    assert calls["session_args"][0].user_data_dir.endswith("profile-test-scenario-1")
    assert calls["session_args"][0].cdp_port == 9555
    assert args.temperature == 0.0
    assert args.show_state is False
    assert args.hide_snapshot is False
    assert args.show_tools is False
    assert args.json is False
    assert args.compress_tools is False
    assert args.cdp_timeout == 30.0


def test_session_factory_uses_distinct_profile_dirs_and_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = run_batch_script.build_parser()
    args = parser.parse_args(
        [
            "--tasks",
            "tasks.jsonl",
            "--model",
            "test-model",
            "--chrome-path",
            "chrome.exe",
            "--user-data-dir",
            "profile",
            "--cdp-port",
            "9555",
        ]
    )
    monkeypatch.setattr(run_batch_script, "_is_port_open", lambda port: port == 9555)
    received_args: list[argparse.Namespace] = []

    def session_builder(received: argparse.Namespace) -> FakeSession:
        received_args.append(received)
        return _fake_session_from_args(received)

    factory = run_batch_script._session_factory(
        args,
        session_builder,  # type: ignore[arg-type]
    )

    first = factory()
    second = factory()

    assert first.config.user_data_dir.endswith("profile-scenario-1")
    assert second.config.user_data_dir.endswith("profile-scenario-2")
    assert first.config.cdp_port == 9556
    assert second.config.cdp_port == 9557
    assert [item.cdp_port for item in received_args] == [9556, 9557]


def test_main_prints_batch_result_without_real_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text('{"scenario_id":"s1","tasks":["inspect"]}\n', encoding="utf-8")

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


def _fake_session_from_args(args: argparse.Namespace) -> FakeSession:
    return FakeSession(
        FakeConfig(
            model=args.model,
            no_mcp=args.no_mcp,
            recursion_limit=args.recursion_limit,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            cdp_port=args.cdp_port,
        )
    )
