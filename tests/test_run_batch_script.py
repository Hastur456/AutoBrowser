from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import run_batch as run_batch_script
from src.agent_loop.batch import BatchScenario

GIT = {"commit": "a" * 40, "branch": "main", "dirty": False}


@dataclass(frozen=True)
class FakeConfig:
    model: str
    no_mcp: bool
    turn_cap: int
    chrome_path: str
    user_data_dir: str
    cdp_port: int


class FakeSession:
    def __init__(self, config: FakeConfig) -> None:
        self.config = config


@pytest.mark.asyncio
async def test_run_batch_from_args_passes_session_factory_and_batch_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_batch_script, "_git_info", lambda: dict(GIT))
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
            "--turn-cap",
            "7",
            "--chrome-path",
            "chrome-test.exe",
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
        session = kwargs["session_factory"](BatchScenario("s1", ["inspect"]))
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
    assert calls["batch_kwargs"]["history"] is True
    assert calls["batch_kwargs"]["config"] == {
        "model": "test-model",
        "no_mcp": True,
        "turn_cap": 7,
        "chrome_path": "chrome-test.exe",
        "cdp_port": 9555,
    }
    batch_id = calls["batch_kwargs"]["batch_id"]
    profile = Path(calls["session_args"][0].user_data_dir)
    assert len(calls["session_args"]) == 1
    assert calls["session_args"][0] is not args
    assert profile.parts[-3:] == (batch_id, "profiles", "scenario-1")
    assert not profile.parent.exists()  # deleted when the batch ends
    assert calls["session_args"][0].cdp_port == 9555
    assert calls["session_args"][0].labels == {
        "autobrowser.batch.id": batch_id,
        "autobrowser.scenario.id": "s1",
        "vcs.ref.head.revision": GIT["commit"],
    }
    provenance = calls["batch_kwargs"]["provenance"]
    assert provenance["git"] == GIT
    assert provenance["model"] == "test-model"
    assert provenance["tasks_sha256"] == run_batch_script._tasks_sha256(tasks_path)
    assert {"provider", "settings_sha256", "non_default_sections", "python", "platform"} <= set(provenance)
    assert args.temperature == 0.0
    assert args.show_state is False
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
        Path("batches") / "batch-1" / "profiles",
        {"autobrowser.batch.id": "batch-1"},
    )

    first = factory(BatchScenario("s1", ["a"]))
    second = factory(BatchScenario("s2", ["b"]))

    assert Path(first.config.user_data_dir) == Path("batches/batch-1/profiles/scenario-1")
    assert Path(second.config.user_data_dir) == Path("batches/batch-1/profiles/scenario-2")
    assert first.config.cdp_port == 9556
    assert second.config.cdp_port == 9557
    assert [item.cdp_port for item in received_args] == [9556, 9557]
    assert [item.labels for item in received_args] == [
        {"autobrowser.batch.id": "batch-1", "autobrowser.scenario.id": "s1"},
        {"autobrowser.batch.id": "batch-1", "autobrowser.scenario.id": "s2"},
    ]


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
            turn_cap=args.turn_cap,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            cdp_port=args.cdp_port,
        )
    )


@pytest.mark.parametrize(
    "result, grade, expected_code",
    [
        ({"status": "completed", "grades": {"passed": 2, "failed": 0, "ungraded": 1}}, True, 0),
        ({"status": "completed", "grades": {"passed": 1, "failed": 1, "ungraded": 0}}, True, 1),
        ({"status": "failed", "grades": {"passed": 0, "failed": 1, "ungraded": 0}}, True, 1),
        ({"status": "failed"}, False, 0),
    ],
)
def test_main_exits_1_only_for_a_failed_grade_or_scenario_under_grade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: dict[str, Any],
    grade: bool,
    expected_code: int,
) -> None:
    received: dict[str, Any] = {}

    async def fake_run_batch_from_args(args: argparse.Namespace) -> dict[str, Any]:
        received["grade"] = args.grade
        return result

    monkeypatch.setattr(run_batch_script, "run_batch_from_args", fake_run_batch_from_args)
    argv = ["--tasks", str(tmp_path / "tasks.jsonl"), "--no-mcp", *(["--grade"] if grade else [])]

    assert run_batch_script.main(argv) == expected_code
    assert received["grade"] is grade


def test_the_parsed_args_build_a_real_session(tmp_path: Path) -> None:
    """The real session builder reads every attribute it needs from run_batch's parser."""

    from src.cli.bootstrap import build_session

    args = run_batch_script.build_parser().parse_args(
        ["--tasks", str(tmp_path / "tasks.jsonl"), "--no-mcp"]
    )

    factory = run_batch_script._session_factory(args, build_session, tmp_path / "profiles", {})
    session = factory(BatchScenario("s1", ["a"]))

    assert session.config.no_mcp is True
    assert Path(session.config.user_data_dir) == tmp_path / "profiles" / "scenario-1"
    # Nobody answers approvals in a batch: the default mode runs as dont_ask.
    assert session.config.permission_mode == "dont_ask"
    assert session.config.labels == {"autobrowser.scenario.id": "s1"}


def test_the_tasks_hash_ignores_line_endings(tmp_path: Path) -> None:
    lf = tmp_path / "lf.jsonl"
    crlf = tmp_path / "crlf.jsonl"
    lf.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    crlf.write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n')

    assert run_batch_script._tasks_sha256(lf) == run_batch_script._tasks_sha256(crlf)
    assert len(run_batch_script._tasks_sha256(lf)) == 64


def test_no_history_skips_the_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_batch_script, "_git_info", lambda: dict(GIT))
    args = run_batch_script.build_parser().parse_args(
        ["--tasks", str(tmp_path / "tasks.jsonl"), "--no-mcp", "--no-history"]
    )
    (tmp_path / "tasks.jsonl").write_text('{"scenario_id":"s1","tasks":["a"]}\n', encoding="utf-8")
    received: dict[str, Any] = {}

    async def batch_runner(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {}

    import asyncio

    asyncio.run(run_batch_script.run_batch_from_args(args, batch_runner=batch_runner))

    assert received["history"] is False


def test_git_info_reads_the_checkout() -> None:
    info = run_batch_script._git_info()

    assert set(info) == {"commit", "branch", "dirty"}
    assert info["commit"] is None or len(info["commit"]) == 40


def test_git_info_is_null_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(run_batch_script.subprocess, "run", missing)

    assert run_batch_script._git_info() == {"commit": None, "branch": None, "dirty": None}
