"""``autobrowser doctor | sessions | replay | recordings | serve | completion`` (§7.6 of the terminal UI design).

The entry point dispatches here when the first argument is one of :data:`SUBCOMMANDS`; a task
that is literally one of these words goes through ``--task`` or after ``--``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.agent_loop.replay import load_events, print_action_sequence, summarize_trace
from src.cli.sessions import session_rows
from src.config import (
    CONFIG_FILE_ENV_VAR,
    active_config_file,
    DEFAULT_CONFIG_FILE_YAML,
    LEGACY_STORAGE_ROOT,
    ROOT_PATH,
    LLMSettings,
    EvalPlatformSettings,
    Settings,
    TelemetrySettings,
    env_files,
    get_settings,
    home_dir,
)
from src.harness.session_store import (
    EVENTS_FILE_NAME,
    SessionNotFoundError,
    find_session,
    list_sessions,
)
from src.harness.recording_store import RecordingNotFoundError, RecordingStore
from src.harness.otel_sink import invalid_otlp_headers, otlp_exporter_installed
from src.harness.skills import SESSION_CAPABILITIES, SkillStore, skill_roots, user_skills_root
from src.version import PACKAGE_NAME, package_version

SUBCOMMANDS = ("doctor", "sessions", "replay", "recordings", "serve", "completion")
SHELLS = ("bash", "zsh", "fish", "tcsh", "powershell")

#: Check marks of ``doctor``: fine, worth a look, broken.
_OK, _WARN, _FAIL = "ok", "!!", "xx"


def run_subcommand(argv: Sequence[str], *, write: Callable[[str], None] = print) -> int:
    _tolerate_unencodable_output()
    parser = build_subcommand_parser()
    args = parser.parse_args(list(argv))
    handler: Callable[[argparse.Namespace, Callable[[str], None]], int] = args.handler
    return handler(args, write)


def _tolerate_unencodable_output() -> None:
    """A redirected stdout uses the legacy code page on Windows (cp1251, cp1252), which has
    no ``·`` or ``×``: replace such characters instead of failing mid-report."""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def build_subcommand_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PACKAGE_NAME)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor", help="Check paths, configuration, the model provider, Node.js, Chrome and the terminal."
    )
    doctor.add_argument(
        "--llm",
        action="store_true",
        help="Also ask the model provider over the network for its model list (sends the key).",
    )
    doctor.set_defaults(handler=_doctor)

    sessions = commands.add_parser("sessions", help="List stored sessions, the most recent first.")
    sessions.add_argument("--all", action="store_true", help="List every session, not only the latest.")
    sessions.set_defaults(handler=_sessions)

    replay = commands.add_parser("replay", help="Print the action sequence of a session.")
    target = replay.add_argument("target", help="Session id or prefix, a session folder, or an events.jsonl file.")
    target.completer = session_id_completer  # type: ignore[attr-defined]  # argcomplete
    replay.set_defaults(handler=_replay)

    recordings = commands.add_parser(
        "recordings", help="List, show or remove the recordings replay uses (replay.dir)."
    )
    actions = recordings.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="List the stored recordings.").set_defaults(handler=_recordings_list)
    show = actions.add_parser("show", help="Print the steps of a recording.")
    show.add_argument("name")
    show.set_defaults(handler=_recordings_show)
    remove = actions.add_parser("rm", help="Remove a recording.")
    remove.add_argument("name")
    remove.set_defaults(handler=_recordings_rm)

    serve = commands.add_parser(
        "serve", help="Run the stand: configured jobs on triggers, the HTTP API (serve.*)."
    )
    serve.add_argument(
        "--check", action="store_true", help="Check the configuration and exit without a browser."
    )
    serve.set_defaults(handler=_serve)

    completion = commands.add_parser("completion", help="Print the shell completion script.")
    completion.add_argument("shell", choices=SHELLS)
    completion.set_defaults(handler=_completion)
    return parser


# -- serve -----------------------------------------------------------------------------------


def _serve(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    # Imported here: the stand pulls in the session, the browser server and uvicorn.
    from src.cli.serve import run_serve_cli

    return run_serve_cli(args, write)


# -- doctor ----------------------------------------------------------------------------------


def _doctor(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    from src.ui.launch import detect

    failures = 0

    def check(mark: str, label: str, value: object) -> None:
        nonlocal failures
        failures += mark == _FAIL
        write(f"{mark}  {label:<14} {value}")

    write(f"{PACKAGE_NAME} {package_version() or 'unknown'} · Python {sys.version.split()[0]}")
    home = home_dir()
    check(_OK if home.is_dir() else _WARN, "home", f"{home}{'' if home.is_dir() else ' (not created yet)'}")
    found = [str(path) for path in env_files() if path.is_file()]
    check(_OK, ".env", ", ".join(found) or "none")
    try:
        config_path = active_config_file()
    except FileNotFoundError as exc:
        check(_FAIL, "config file", exc)
        return 1
    else:
        if config_path is None:
            check(_OK, "config file", "none")
        else:
            source = (
                CONFIG_FILE_ENV_VAR
                if os.environ.get(CONFIG_FILE_ENV_VAR)
                else "repo, legacy" if config_path == DEFAULT_CONFIG_FILE_YAML else "home"
            )
            check(_OK, "config file", f"{config_path} ({source})")
    # The checks above need no settings: they still run when the settings do not load.
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - doctor reports a broken configuration
        check(_FAIL, "settings", f"{type(exc).__name__}: {exc}")
        return 1
    storage = settings.storage
    check(_OK, "storage", storage.root_dir)
    check(_OK, "sessions", f"{storage.sessions_dir} ({len(list_sessions(storage.sessions_dir))})")
    check(_OK, "log file", settings.ui.resolved_log_file(storage))
    legacy = ROOT_PATH / LEGACY_STORAGE_ROOT
    if legacy.is_dir() and legacy.resolve() != storage.root_dir.resolve():
        check(
            _WARN,
            "old data",
            f"{legacy} — set storage.root_dir to it or move its sessions/ and memory/ to {storage.root_dir}",
        )

    timeout = settings.ui.doctor_probe_timeout_seconds
    llm_mark, llm_line = _llm_summary(settings.llm, settings.loop.progress_timeout_seconds)
    check(llm_mark, "llm", llm_line)
    if getattr(args, "llm", False):
        check(*_llm_probe(settings.llm, timeout))
    for tool in ("node", "npx"):
        path = shutil.which(tool)
        if path is None:
            check(_FAIL if settings.mcp_servers else _WARN, tool, "not on PATH (Playwright MCP needs Node.js >= 18)")
            continue
        check(_OK, tool, f"{_version(path, timeout)} · {path}")
    browser = settings.browser
    if browser.launch == "external":
        check(_OK, "browser launch", "external (the MCP server launches its own browser)")
    else:
        check(_OK, "browser launch", "cdp, headless" if browser.headless else "cdp")
        chrome = Path(browser.chrome_path)
        check(_OK if chrome.is_file() else _FAIL, "chrome", chrome)
        port = browser.cdp_port
        free = _port_free(port)
        check(_OK if free else _WARN, "cdp port", f"{port}" if free else f"{port} in use (a Chrome may already listen)")
    servers = ", ".join(f"{name} ({config.transport})" for name, config in settings.mcp_servers.items())
    check(_OK if servers else _WARN, "mcp servers", servers or "none configured")
    _doctor_skills(settings, check)
    check(*_telemetry_summary(settings.telemetry))
    check(*_eval_platform_summary(settings.eval_platform))

    caps = detect(settings.ui)
    check(
        _OK if caps.level != "plain" else _WARN,
        "terminal",
        f"{caps.level} · {caps.host} · colors {caps.color} · unicode {caps.unicode} · {caps.reason}",
    )
    return 1 if failures else 0


def _doctor_skills(settings: Settings, check: Callable[[str, str, object], None]) -> None:
    """Skills of every capability (doctor knows no session): counts, shadowed built-ins,
    skipped skills and frontmatter keys outside the Agent Skills format."""

    skills = settings.skills
    store = SkillStore.load(
        skills,
        roots=skill_roots(skills, settings.storage),
        capabilities=SESSION_CAPABILITIES,
    )
    builtin = sum(1 for skill in store.skills() if skill.source == "builtin")
    user = [skill for skill in store.skills() if skill.source == "user"]
    folder = user_skills_root(skills, settings.storage)
    line = f"{'on' if skills.enabled else 'off'} · {builtin} built-in"
    if skills.enabled:
        line += f" · {len(user)} yours ({folder}{'' if folder.is_dir() else ', not created yet'})"
    if store.shadowed():
        line += f" · yours replace {', '.join(store.shadowed())}"
    check(_WARN if store.problems() else _OK, "skills", line)
    for problem in store.problems():
        check(_WARN, "skill", f"{problem.source}/{problem.path} skipped: {problem.reason}")
    for skill in store.skills():
        if skill.unknown_fields:
            check(_WARN, "skill", f"{skill.name}: unknown frontmatter keys {', '.join(skill.unknown_fields)}")


def _eval_platform_summary(platform: EvalPlatformSettings) -> tuple[str, str, str]:
    """The evaluation-platform line, offline: host without credentials, whether both keys are
    set (never their values) and the content mode. The network check is the platform's own
    sync tool."""

    if not platform.enabled:
        return _OK, "eval platform", "disabled"
    keys = bool(
        platform.public_key
        and platform.public_key.get_secret_value().strip()
        and platform.secret_key
        and platform.secret_key.get_secret_value().strip()
    )
    host = _public_url(platform.host)
    parts = [
        "enabled",
        f"host {host}" if host else "no host",
        "keys set" if keys else "keys missing",
        f"content {platform.capture_content}",
    ]
    return (_OK if keys and host else _WARN), "eval platform", " · ".join(parts)


def _telemetry_summary(telemetry: TelemetrySettings) -> tuple[str, str, str]:
    """The OTLP export line, offline: endpoint without credentials, content mode, the number
    of headers (never their names or values) and whether the exporter package is installed."""

    if not telemetry.otlp_enabled:
        return _OK, "telemetry", "off"
    headers = len(telemetry.otlp_headers)
    parts = [
        _public_url(telemetry.otlp_endpoint) or "",
        f"content {telemetry.capture_content}",
        f"{headers} header{'' if headers == 1 else 's'}",
    ]
    invalid = invalid_otlp_headers(telemetry)
    if invalid:
        return _FAIL, "telemetry", " · ".join([*parts, f"malformed header {', '.join(invalid)}"])
    if otlp_exporter_installed():
        return _OK, "telemetry", " · ".join([*parts, "exporter installed"])
    return _FAIL, "telemetry", " · ".join([*parts, "exporter missing: uv sync --group otel"])


def _llm_summary(llm: LLMSettings, stall_budget: float) -> tuple[str, str]:
    """The provider line, offline: protocol, URL, model, key set or not, call budget.

    Never prints the key or a header value. Warns when one model call can outlast the goal
    stall budget (``timeout_seconds x (max_retries + 1)``).
    """

    key = "key set" if llm.api_key and llm.api_key.get_secret_value().strip() else "no key"
    if llm.timeout_seconds is None:
        budget, worst = "no timeout", None
    else:
        budget = f"timeout {llm.timeout_seconds:g} s × {llm.max_retries + 1}"
        worst = llm.timeout_seconds * (llm.max_retries + 1)
    parts = [llm.provider, _public_url(llm.host) or "client default", llm.model, key, budget]
    if llm.extra_headers:
        parts.append("headers " + ", ".join(sorted(llm.extra_headers)))
    line = " · ".join(parts)
    if worst is None or worst >= stall_budget:
        return _WARN, f"{line} — one call may outlast loop.progress_timeout_seconds ({stall_budget:g} s)"
    return _OK, line


def _public_url(url: str | None) -> str | None:
    """``url`` without userinfo, query and fragment, which may carry credentials."""

    if not url:
        return url
    parts = urlsplit(url)
    if not parts.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _llm_probe(llm: LLMSettings, timeout: float) -> tuple[str, str, str]:
    """``doctor --llm``: list the provider's models once, without retries."""

    from src.providers.registry import probe_chat_provider

    probe = probe_chat_provider(llm, timeout=timeout)
    if not probe.reachable:
        return _FAIL, "llm probe", probe.error or "unreachable"
    if probe.models_count is None:
        return _OK, "llm probe", "reachable · no model list"
    listed = f"{probe.models_count} models"
    if probe.model_listed:
        return _OK, "llm probe", f"reachable · {listed} · {llm.model} listed"
    return _WARN, "llm probe", f"reachable · {listed} · {llm.model} not listed (aliases are not shown)"


def _version(executable: str, timeout: float) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({type(exc).__name__})"
    lines = (completed.stdout or completed.stderr or "").strip().splitlines()
    return lines[0] if lines else "unknown"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# -- sessions / replay -----------------------------------------------------------------------


def _sessions(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    settings = get_settings()
    sessions = list_sessions(settings.storage.sessions_dir)
    if not args.all:
        sessions = sessions[: settings.ui.resume_list_size]
    if not sessions:
        write(f"No sessions in {settings.storage.sessions_dir}.")
        return 0
    for row in session_rows(sessions):
        write(row)
    return 0


def _replay(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    try:
        trace = _trace_path(args.target)
    except (FileNotFoundError, SessionNotFoundError) as exc:
        write(str(exc))
        return 1
    events = load_events(trace)
    summary = summarize_trace(events)
    write(
        f"{trace} · {summary.terminal_status} · {summary.model_turn_count} model turns · "
        f"{summary.tool_call_count} tool calls"
    )
    write(print_action_sequence(events))
    return 0


def _trace_path(target: str) -> Path:
    path = Path(target).expanduser()
    if path.is_file():
        return path
    if path.is_dir() and (path / EVENTS_FILE_NAME).is_file():
        return path / EVENTS_FILE_NAME
    session = find_session(get_settings().storage.sessions_dir, target)
    trace = session.path / EVENTS_FILE_NAME
    if not trace.is_file():
        raise FileNotFoundError(f"Session {session.session_id[:12]} has no {EVENTS_FILE_NAME}.")
    return trace


# -- recordings ------------------------------------------------------------------------------


def _recording_store() -> RecordingStore:
    settings = get_settings()
    return RecordingStore.from_settings(settings.replay, settings.storage)


def _recordings_list(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    store = _recording_store()
    entries = store.entries()
    if not entries:
        write(f"No recordings in {store.root}.")
        return 0
    for entry in entries:
        if entry.error:
            write(f"{entry.name:<24} unreadable: {entry.error}")
        else:
            write(f"{entry.name:<24} {entry.steps:>3} steps  {entry.created_at[:19]}  {entry.task}")
    return 0


def _recordings_show(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    store = _recording_store()
    try:
        recording = store.load(args.name)
    except (RecordingNotFoundError, ValueError) as exc:
        write(str(exc))
        return 1
    limit = store.settings.detail_max_chars
    write(f"{recording.name} · {len(recording.steps)} steps · answer: {recording.answer}")
    write(f"task: {recording.task}")
    stats = store.stats(args.name)
    if stats:
        write(f"played {stats.get('playbacks', 0)} times, last stage: {stats.get('last_stage', '-')}")
    for step in recording.steps:
        args_text = json.dumps(dict(step.args), ensure_ascii=False)[:limit]
        marks = [f"{len(step.bindings)} refs"] if step.bindings else []
        if step.redacted:
            marks.append("redacted")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        write(f"{step.index:>3}. {step.tool} {args_text}{suffix}")
    return 0


def _recordings_rm(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    try:
        removed = _recording_store().delete(args.name)
    except ValueError as exc:
        write(str(exc))
        return 1
    write(f"Removed {args.name}." if removed else f"No recording named {args.name!r}.")
    return 0 if removed else 1


# -- completion ------------------------------------------------------------------------------


def _completion(args: argparse.Namespace, write: Callable[[str], None]) -> int:
    import argcomplete

    write(argcomplete.shellcode([PACKAGE_NAME], shell=args.shell))
    return 0


def session_id_completer(prefix: str, **_kwargs: object) -> list[str]:
    """argcomplete completer for ``--resume`` and ``replay``: stored session ids."""

    return [
        session.session_id
        for session in list_sessions(get_settings().storage.sessions_dir)
        if session.session_id.startswith(prefix)
    ]


__all__ = ["SHELLS", "SUBCOMMANDS", "build_subcommand_parser", "run_subcommand", "session_id_completer"]
