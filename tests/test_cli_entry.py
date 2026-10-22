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


def test_doctor_reports_a_missing_config_file_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(tmp_path / "missing.yaml"))
    get_settings.cache_clear()

    assert entry.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "xx  config file" in out and "missing.yaml" in out


def test_replay_completes_session_ids() -> None:
    parser = subcommands.build_subcommand_parser()
    replay = next(action for action in parser._subparsers._group_actions[0].choices["replay"]._actions  # type: ignore[union-attr]
                  if action.dest == "target")

    assert replay.completer is subcommands.session_id_completer  # type: ignore[attr-defined]


# -- the model provider: --provider, the doctor llm line, doctor --llm -----------------------


def use_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    profile = tmp_path / "llm-profile.yaml"
    profile.write_text(text, encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()


def doctor_line(out: str, label: str) -> str:
    return next(line for line in out.splitlines() if line[4:].startswith(f"{label} "))


def test_provider_flag_defaults_to_the_setting_and_rejects_unknown_protocols() -> None:
    assert build_parser().parse_args([]).provider is None
    assert build_parser().parse_args(["--provider", "openai_compat"]).provider == "openai_compat"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--provider", "anthropic"])


def test_build_session_binds_the_provider_flag_to_the_registry() -> None:
    import argparse

    from src.cli import bootstrap
    from src.providers.registry import build_chat_model

    args = argparse.Namespace(
        task=[], task_text=None, loop=False, model="m", temperature=None, show_state=False,
        show_tools=False, json=False, no_mcp=True, compress_tools=False, agent_loop=False,
        chrome_path="chrome.exe", user_data_dir="profile", cdp_port=9222, cdp_timeout=1,
        turn_cap=3, interactive=False, permission_mode=None, provider="openai_compat",
    )

    session = bootstrap.build_session(args)

    factory = session._llm_factory
    assert factory.func is build_chat_model
    assert factory.keywords == {"provider": "openai_compat"}


def test_doctor_prints_the_provider_without_secrets_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(
        monkeypatch,
        tmp_path,
        "llm:\n"
        "  provider: openai_compat\n"
        "  host: http://llm.test/v1\n"
        "  model: gemma-4\n"
        "  api_key: sk-secret\n"
        "  extra_headers: {X-Api-Key: hdr-secret}\n",
    )
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor without --llm must not probe the provider")

    monkeypatch.setattr("src.providers.registry.probe_chat_provider", no_network)

    entry.main(["doctor"])

    out = capsys.readouterr().out
    line = doctor_line(out, "llm")
    assert line.startswith("ok  ")
    for part in ("openai_compat", "http://llm.test/v1", "gemma-4", "key set", "timeout 60 s × 1", "X-Api-Key"):
        assert part in line
    assert "sk-secret" not in out and "hdr-secret" not in out
    assert "llm probe" not in out


def test_doctor_warns_when_a_model_call_can_outlast_the_stall_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(monkeypatch, tmp_path, "llm:\n  timeout_seconds: 60\n  max_retries: 1\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    line = doctor_line(capsys.readouterr().out, "llm")
    assert line.startswith("!!  ")
    assert "loop.progress_timeout_seconds" in line


@pytest.mark.parametrize(
    "url, shown",
    [
        (None, None),
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("https://user:token@llm.test/v1?api-key=sk-x#frag", "https://llm.test/v1"),
        ("localhost:8000/v1?key=sk-x", "localhost:8000/v1"),
    ],
)
def test_doctor_shows_the_host_without_credentials(url: str | None, shown: str | None) -> None:
    assert subcommands._public_url(url) == shown


@pytest.mark.parametrize(
    "probe, mark, text, failed",
    [
        ({"reachable": True, "model_listed": True, "models_count": 51}, "ok", "reachable · 51 models · gemma-4 listed", False),
        ({"reachable": True, "model_listed": False, "models_count": 3}, "!!", "gemma-4 not listed", False),
        ({"reachable": False, "error": "openai_compat 401: authentication failed"}, "xx", "authentication failed", True),
    ],
)
def test_doctor_llm_probes_the_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    probe: dict[str, object],
    mark: str,
    text: str,
    failed: bool,
) -> None:
    from src.providers.registry import ProviderProbe

    use_profile(monkeypatch, tmp_path, "llm:\n  model: gemma-4\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)
    calls: list[float] = []

    def fake_probe(settings: object, *, timeout: float) -> ProviderProbe:
        calls.append(timeout)
        return ProviderProbe(**probe)  # type: ignore[arg-type]

    monkeypatch.setattr("src.providers.registry.probe_chat_provider", fake_probe)

    code = entry.main(["doctor", "--llm"])

    line = doctor_line(capsys.readouterr().out, "llm probe")
    assert line.startswith(f"{mark}  ")
    assert text in line
    assert calls == [get_settings().ui.doctor_probe_timeout_seconds]
    if failed:
        assert code == 1
