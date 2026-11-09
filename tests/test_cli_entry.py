"""The ``autobrowser`` entry point: subcommand dispatch, ``sessions``, ``replay``,
``completion``, ``doctor`` and ``--version``."""

from __future__ import annotations

import io
import json
import sys
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


def test_doctor_reports_the_browser_launch_mode(capsys: pytest.CaptureFixture[str]) -> None:
    entry.main(["doctor"])

    assert "browser launch" in capsys.readouterr().out


def test_doctor_skips_chrome_when_the_mcp_server_owns_the_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_BROWSER__LAUNCH", "external")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__CHROME_PATH", "C:/nowhere/chrome.exe")
    get_settings.cache_clear()

    entry.main(["doctor"])

    out = capsys.readouterr().out
    assert "external" in out
    assert "nowhere" not in out
    assert "cdp port" not in out


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
    from src.providers.registry import build_chat_model, list_chat_models

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
    catalog = session._model_catalog
    assert catalog.func is list_chat_models
    assert catalog.keywords == {"provider": "openai_compat"}
    assert session.config.provider == "openai_compat"


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


def test_doctor_reports_telemetry_off_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    assert doctor_line(capsys.readouterr().out, "telemetry") == "ok  telemetry      off"


TELEMETRY_PROFILE = (
    "telemetry:\n"
    "  otlp_enabled: true\n"
    "  otlp_endpoint: https://user:pw@otel.test/v1/traces?token=q-secret\n"
    "  capture_content: redacted\n"
    "  otlp_headers: {Authorization: Basic hdr-secret, X-Ingest: v-secret}\n"
)


@pytest.mark.parametrize("installed, mark, state", [(True, "ok", "exporter installed"), (False, "xx", "uv sync --group otel")])
def test_doctor_reports_telemetry_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    installed: bool,
    mark: str,
    state: str,
) -> None:
    use_profile(monkeypatch, tmp_path, TELEMETRY_PROFILE)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)
    monkeypatch.setattr(subcommands, "otlp_exporter_installed", lambda: installed)

    code = entry.main(["doctor"])

    out = capsys.readouterr().out
    line = doctor_line(out, "telemetry")
    assert line.startswith(f"{mark}  ")
    for part in ("https://otel.test/v1/traces", "content redacted", "2 headers", state):
        assert part in line
    for secret in ("user:pw", "q-secret", "hdr-secret", "v-secret", "Authorization"):
        assert secret not in line
    assert "hdr-secret" not in out and "v-secret" not in out
    if not installed:
        assert code == 1


def test_doctor_fails_on_a_malformed_telemetry_header_without_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(monkeypatch, tmp_path, "telemetry:\n  otlp_enabled: true\n  otlp_headers: {Authorization: ' Basic hdr-secret'}\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)
    monkeypatch.setattr(subcommands, "otlp_exporter_installed", lambda: True)

    assert entry.main(["doctor"]) == 1

    out = capsys.readouterr().out
    line = doctor_line(out, "telemetry")
    assert line.startswith("xx  ") and "malformed header Authorization" in line
    assert "hdr-secret" not in out


def test_doctor_reports_the_eval_platform_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    assert doctor_line(capsys.readouterr().out, "eval platform") == "ok  eval platform  disabled"


@pytest.mark.parametrize(
    "keys, mark, state",
    [
        ("  public_key: pk-secret-1\n  secret_key: sk-secret-2\n", "ok", "keys set"),
        ("  public_key: pk-secret-1\n", "!!", "keys missing"),
    ],
)
def test_doctor_reports_the_eval_platform_offline_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    keys: str,
    mark: str,
    state: str,
) -> None:
    use_profile(
        monkeypatch,
        tmp_path,
        "eval_platform:\n  enabled: true\n  host: https://platform.example.test\n  capture_content: answers\n" + keys,
    )
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    out = capsys.readouterr().out
    line = doctor_line(out, "eval platform")
    assert line.startswith(f"{mark}  ")
    for part in ("enabled", "host https://platform.example.test", state, "content answers"):
        assert part in line
    assert "pk-secret-1" not in out and "sk-secret-2" not in out


def test_doctor_warns_when_the_enabled_eval_platform_has_no_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(monkeypatch, tmp_path, "eval_platform:\n  enabled: true\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    line = doctor_line(capsys.readouterr().out, "eval platform")
    assert line.startswith("!!  ") and "no host" in line


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


CASCADE_PROFILE = (
    "llm:\n"
    "  model: cheap\n"
    "  prices: {cheap: {input_per_mtok: 1, output_per_mtok: 1}}\n"
    "cascade:\n"
    "  enabled: true\n"
    "  tiers:\n"
    "    - {model: strong, label: big}\n"
    "    - {model: gpt, provider: openai_compat, host: 'https://user:pw@api.test/v1?k=q', api_key: sk-tier}\n"
)


def test_doctor_prints_the_cascade_tiers_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(monkeypatch, tmp_path, CASCADE_PROFILE)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    out = capsys.readouterr().out
    tiers = [line for line in out.splitlines() if line[4:].startswith("cascade ")]
    assert len(tiers) == 2
    assert "big" in tiers[0] and "strong" in tiers[0] and "no price" in tiers[0]
    assert tiers[0].startswith("!!")
    assert "https://api.test/v1" in tiers[1] and "key set" in tiers[1]
    assert "sk-tier" not in out and "user:pw" not in out and "k=q" not in out


def test_doctor_has_no_cascade_line_while_it_is_off(capsys: pytest.CaptureFixture[str]) -> None:
    entry.main(["doctor"])

    assert not any(line[4:].startswith("cascade ") for line in capsys.readouterr().out.splitlines())


def test_doctor_fails_a_tier_that_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(
        monkeypatch,
        tmp_path,
        "cascade:\n  enabled: true\n  tiers: [{model: gpt, provider: openai_compat, api_key: sk-tier}]\n",
    )
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    assert entry.main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert doctor_line(out, "cascade").startswith("xx")
    assert "sk-tier" not in out


def test_doctor_llm_probes_each_tier_on_another_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.providers.registry import ProviderProbe

    use_profile(monkeypatch, tmp_path, CASCADE_PROFILE)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)
    hosts: list[object] = []

    def fake_probe(settings: object, *, timeout: float) -> ProviderProbe:
        hosts.append(getattr(settings, "host", None))
        return ProviderProbe(reachable=True)

    monkeypatch.setattr("src.providers.registry.probe_chat_provider", fake_probe)

    entry.main(["doctor", "--llm"])

    # The session's provider, then only the tier with another (provider, host).
    assert hosts == [None, "https://user:pw@api.test/v1?k=q"]
    assert "cascade probe" in capsys.readouterr().out


def test_doctor_reports_skills_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_profile(monkeypatch, tmp_path, "skills:\n  enabled: false\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    assert doctor_line(capsys.readouterr().out, "skills") == "ok  skills         off · 1 built-in"


def test_doctor_reports_user_skills_shadowing_and_problems(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.test_skills import write_skill

    folder = tmp_path / "my-skills"
    write_skill(folder, "pdf-forms", metadata="version: 2")
    write_skill(folder, "browser-agent-rules")
    write_skill(folder, "broken", frontmatter="name: broken")
    use_profile(monkeypatch, tmp_path, f"skills:\n  enabled: true\n  dir: '{folder.as_posix()}'\n")
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    entry.main(["doctor"])

    out = capsys.readouterr().out
    line = doctor_line(out, "skills")
    assert line.startswith("!")
    for part in ("on · 0 built-in", "2 yours", str(folder), "yours replace browser-agent-rules"):
        assert part in line, part
    assert "user/broken skipped: description is missing" in out
    assert "pdf-forms: unknown frontmatter keys version" in out


def test_doctor_survives_a_legacy_code_page_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(subcommands.shutil, "which", lambda name: None)

    code = entry.main(["doctor"])

    stream.flush()
    assert code in (0, 1)
    assert b"skills" in raw.getvalue()


# -- recordings ------------------------------------------------------------------------------


def enable_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "replay.yaml"
    profile.write_text("replay: {enabled: true}\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()


def save_recording(name: str) -> None:
    from src.agent_loop.recordings import RecordedStep, Recording
    from src.harness.recording_store import RecordingStore

    settings = get_settings()
    RecordingStore.from_settings(settings.replay, settings.storage).save(
        Recording(
            name=name,
            task="read the price",
            steps=(
                RecordedStep(index=1, tool="open", args={"url": "https://shop.example"}),
                RecordedStep(index=2, tool="fill", args={"password": "[REDACTED]"}, redacted=True),
            ),
            created_at="2026-11-02T10:00:00+00:00",
        )
    )


def test_recordings_list_show_and_rm(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["recordings", "list"]) == 0
    assert "No recordings" in capsys.readouterr().out

    save_recording("price-watch")
    assert entry.main(["recordings", "list"]) == 0
    assert "price-watch" in capsys.readouterr().out

    assert entry.main(["recordings", "show", "price-watch"]) == 0
    shown = capsys.readouterr().out
    assert "1. open" in shown and "redacted" in shown and "read the price" in shown

    assert entry.main(["recordings", "rm", "price-watch"]) == 0
    assert entry.main(["recordings", "rm", "price-watch"]) == 1
    assert entry.main(["recordings", "show", "../etc"]) == 1
    assert "invalid recording name" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv, enabled, message",
    [
        (["-p", "--record", "x", "task"], False, "replay.enabled"),
        (["-p", "--record", "Bad Name", "task"], True, "invalid recording name"),
        (["--classic", "--record", "x", "task"], True, "not --classic"),
    ],
)
def test_record_flag_problems_are_usage_errors(
    argv: list[str],
    enabled: bool,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if enabled:
        enable_replay(tmp_path, monkeypatch)

    assert entry.main(argv) == 2
    assert message in capsys.readouterr().err


def test_serve_check_accepts_the_default_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["serve", "--check"]) == 0

    out = capsys.readouterr().out
    assert "127.0.0.1:8765" in out
    assert "0 jobs" in out


def test_serve_check_never_prints_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_SERVE__TOKEN", "t0ken-value")
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 0

    captured = capsys.readouterr()
    assert "t0ken-value" not in captured.out + captured.err
    assert "token set" in captured.out


@pytest.mark.parametrize(
    "env, message",
    [
        ({"AUTOBROWSER_PERMISSIONS__MODE": "bypass"}, "bypass"),
        ({"AUTOBROWSER_SERVE__HOST": "0.0.0.0"}, "allow_remote"),
        ({"AUTOBROWSER_SERVE__JOBS": '{"j": {"task": "t", "recording": "rec"}}'}, "replay.enabled"),
        ({"AUTOBROWSER_SERVE__DAILY_MAX_COST": "1.0"}, "llm.prices"),
    ],
)
def test_serve_check_refuses_an_unsafe_or_broken_stand(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: dict[str, str], message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 2

    captured = capsys.readouterr()
    assert message in captured.out + captured.err


def test_serve_check_refuses_a_broken_cascade_tier_without_its_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_CASCADE__ENABLED", "true")
    monkeypatch.setenv(
        "AUTOBROWSER_CASCADE__TIERS", '[{"model": "gpt", "provider": "openai_compat", "api_key": "sk-tier"}]'
    )
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 2

    captured = capsys.readouterr()
    assert "cascade tier 'gpt'" in captured.err
    assert "sk-tier" not in captured.out + captured.err


def test_serve_check_summary_counts_the_cascade_tiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_CASCADE__ENABLED", "true")
    monkeypatch.setenv("AUTOBROWSER_CASCADE__TIERS", '[{"model": "strong"}]')
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 0

    assert "cascade 1 tiers" in capsys.readouterr().out


def test_the_serve_worker_has_its_own_profile_port_and_labels() -> None:
    from src.cli.serve import worker_args
    from src.config import ServeSettings, StorageSettings
    from src.serve.paths import serve_paths

    serve = ServeSettings(cdp_port_base=9400)
    args = worker_args(serve, serve_paths(serve, StorageSettings(root_dir=Path("C:/stand"))))

    assert Path(args.user_data_dir) == Path("C:/stand/serve/profiles/w0")
    assert args.cdp_port == 9400
    assert args.labels == {"mode": "serve", "worker": "w0"}
    assert args.interactive is False  # no approval channel: dont_ask


@pytest.mark.asyncio
async def test_serve_composes_the_stand_and_writes_its_event_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    from src.cli import serve as serve_cli
    from src.harness.session import SessionRuntime
    from tests.test_harness_session import llm_factory, make_config, no_mcp_runtime, no_start, noop_wait

    built: list[object] = []

    def build_session(args, *, human_input, event_sinks, output_fn):
        built.append(args)
        return SessionRuntime(
            make_config(), llm_factory=llm_factory, start_chrome_cdp=no_start, wait_for_port=noop_wait,
            mcp_runtime_factory=no_mcp_runtime, human_input=human_input, event_sinks=list(event_sinks),
        )

    monkeypatch.setattr(serve_cli, "build_session", build_session)
    settings = get_settings()

    code = await serve_cli._serve(settings, lambda _line: None, until=asyncio.sleep(0))

    assert code == 0
    assert built[0].labels == {"mode": "serve", "worker": "w0"}
    events = (settings.storage.root_dir / "serve" / "events.jsonl").read_text(encoding="utf-8")
    assert '"serve.started"' in events and '"serve.stopped"' in events


@pytest.mark.asyncio
async def test_serve_fires_a_due_interval_job_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    from src.cli import serve as serve_cli
    from src.harness.session import SessionRuntime
    from tests.session_fakes import install_fake_agent
    from tests.test_harness_session import llm_factory, make_config, native_result, no_mcp_runtime, no_start, noop_wait

    tasks: list[str] = []

    async def run(request):
        tasks.append(request.task)
        return native_result(final_answer="price 10")

    install_fake_agent(monkeypatch, run)
    monkeypatch.setattr(
        serve_cli,
        "build_session",
        lambda args, *, human_input, event_sinks, output_fn: SessionRuntime(
            make_config(), llm_factory=llm_factory, start_chrome_cdp=no_start, wait_for_port=noop_wait,
            mcp_runtime_factory=no_mcp_runtime, human_input=human_input, event_sinks=list(event_sinks),
        ),
    )
    monkeypatch.setenv(
        "AUTOBROWSER_SERVE__JOBS",
        '{"price-watch": {"task": "Report the price.", '
        '"triggers": [{"id": "hourly", "type": "interval", "every_seconds": 3600}]}}',
    )
    get_settings.cache_clear()
    settings = get_settings()

    async def until_reported() -> None:
        ledger = settings.storage.root_dir / "serve" / "ledger.jsonl"
        while not ledger.is_file():
            await asyncio.sleep(0.01)

    code = await asyncio.wait_for(serve_cli._serve(settings, lambda _line: None, until=until_reported()), 10)

    assert code == 0
    assert tasks == ["Report the price."]


def test_the_serve_http_server_keeps_client_addresses_out_of_its_log() -> None:
    from src.cli.serve import http_server
    from src.config import ServeSettings

    server = http_server(object(), ServeSettings(port=8799))

    assert server.config.access_log is False
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8799)


def test_serve_builds_the_configured_channels() -> None:
    from src.agent_loop.events import EventEmitter
    from src.cli.serve import build_channels
    from src.config import ServeSettings

    serve = ServeSettings(channels=["log", "webhook"], webhook_out={"url": "https://hooks.example/x"})

    names = [channel.name for channel in build_channels(serve, EventEmitter(), print)]

    assert names == ["log", "webhook"]


def test_an_invalid_stand_configuration_never_prints_a_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOBROWSER_SERVE__HOST", "0.0.0.0")
    monkeypatch.setenv("AUTOBROWSER_SERVE__TOKEN", "SUPERSECRETTOKEN123")
    monkeypatch.setenv(
        "AUTOBROWSER_SERVE__JOBS",
        '{"j": {"task": "t", "triggers": [{"id": "ci", "type": "webhook", "secret": "HOOKSECRET999", "secret_env": "X"}]}}',
    )
    monkeypatch.setenv("AUTOBROWSER_SERVE__WEBHOOK_OUT", '{"url": "ftp://hooks.example/?key=URLSECRET42"}')
    get_settings.cache_clear()

    assert entry.main(["serve", "--check"]) == 2

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "invalid configuration" in output
    for secret in ("SUPERSECRETTOKEN123", "HOOKSECRET999", "URLSECRET42"):
        assert secret not in output
