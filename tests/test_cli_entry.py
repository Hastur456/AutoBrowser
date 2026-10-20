"""The ``autobrowser`` entry point: subcommand dispatch, ``sessions``, ``replay``,
``completion``, ``doctor`` and ``--version``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cli import entry, subcommands
from src.cli.parser import build_parser
from src.config import CONFIG_FILE_ENV_VAR, get_settings
from src.harness.session_store import save_state
from src.messages import user_message


@pytest.fixture(autouse=True)
def _neutral_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("flags: {}\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()


def stored_session(name: str, *, task: str = "найди", events: list[dict[str, object]] | None = None) -> Path:
    folder = get_settings().storage.sessions_dir / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(
        json.dumps({"session_id": name, "metadata": {"last_activity": "2026-10-20T10:00:00+00:00"}}),
        encoding="utf-8",
    )
    (folder / "tasks.json").write_text(json.dumps([{"task": task}]), encoding="utf-8")
    save_state(folder, name, {"messages": [user_message(task)]})
    lines = [json.dumps({"source": "test", **event}) for event in events or []]
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_a_subcommand_word_dispatches_and_anything_else_runs_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[object] = []
    monkeypatch.setattr(entry, "run_subcommand", lambda argv: ran.append(("sub", argv)) or 0)
    monkeypatch.setattr(entry, "run_agent_cli", lambda args: ran.append(("agent", args.task, args.task_text)) or 0)

    assert entry.main(["sessions", "--all"]) == 0
    assert entry.main(["найди", "наушники"]) == 0
    assert entry.main(["--task", "doctor"]) == 0  # the word as a task

    assert ran == [("sub", ["sessions", "--all"]), ("agent", ["найди", "наушники"], None), ("agent", [], "doctor")]


def test_sessions_lists_stored_sessions(capsys: pytest.CaptureFixture[str]) -> None:
    stored_session("abc123def456ffff", task="купи наушники")

    assert entry.main(["sessions"]) == 0

    out = capsys.readouterr().out
    assert "abc123def456" in out and "resume" in out and "купи наушники" in out


def test_replay_by_session_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    stored_session(
        "feed00000001",
        events=[
            {"type": "goal.started", "payload": {"task": "открой"}},
            {"type": "action.proposed", "payload": {"tool_request": {"name": "any_tool", "args": {"x": 1}}}},
            {"type": "goal.completed", "payload": {"status": "completed", "final_answer": "ok"}},
        ],
    )

    assert entry.main(["replay", "feed"]) == 0

    out = capsys.readouterr().out
    assert "completed" in out and "goal.started: открой" in out and '1. any_tool {"x": 1}' in out
    assert entry.main(["replay", "nope"]) == 1


@pytest.mark.parametrize("shell, marker", [("bash", "complete"), ("powershell", "Register-ArgumentCompleter")])
def test_completion_prints_a_shell_script(shell: str, marker: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["completion", shell]) == 0

    out = capsys.readouterr().out
    assert marker in out and "autobrowser" in out


def test_resume_completes_stored_session_ids() -> None:
    stored_session("aaaa1111")
    stored_session("bbbb2222")

    assert subcommands.session_id_completer("aa") == ["aaaa1111"]


def test_doctor_reports_paths_and_tools(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    code = entry.main(["doctor"])

    out = capsys.readouterr().out
    assert code in (0, 1)
    for label in ("home", "config file", "storage", "sessions", "log file", "node", "npx", "chrome", "terminal"):
        assert label in out
    assert "not on PATH" in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("autobrowser ")
