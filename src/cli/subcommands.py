"""``autobrowser doctor | sessions | replay | completion`` (§7.6 of the terminal UI design).

The entry point dispatches here when the first argument is one of :data:`SUBCOMMANDS`; a task
that is literally one of these words goes through ``--task`` or after ``--``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from src.agent_loop.replay import load_events, print_action_sequence, summarize_trace
from src.cli.sessions import session_rows
from src.config import (
    CONFIG_FILE_ENV_VAR,
    active_config_file,
    DEFAULT_CONFIG_FILE_YAML,
    LEGACY_STORAGE_ROOT,
    ROOT_PATH,
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
from src.version import PACKAGE_NAME, package_version

SUBCOMMANDS = ("doctor", "sessions", "replay", "completion")
SHELLS = ("bash", "zsh", "fish", "tcsh", "powershell")

#: Check marks of ``doctor``: fine, worth a look, broken.
_OK, _WARN, _FAIL = "ok", "!!", "xx"


def run_subcommand(argv: Sequence[str], *, write: Callable[[str], None] = print) -> int:
    parser = build_subcommand_parser()
    args = parser.parse_args(list(argv))
    handler: Callable[[argparse.Namespace, Callable[[str], None]], int] = args.handler
    return handler(args, write)


def build_subcommand_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PACKAGE_NAME)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check paths, configuration, Node.js, Chrome and the terminal.")
    doctor.set_defaults(handler=_doctor)

    sessions = commands.add_parser("sessions", help="List stored sessions, the most recent first.")
    sessions.add_argument("--all", action="store_true", help="List every session, not only the latest.")
    sessions.set_defaults(handler=_sessions)

    replay = commands.add_parser("replay", help="Print the action sequence of a session.")
    target = replay.add_argument("target", help="Session id or prefix, a session folder, or an events.jsonl file.")
    target.completer = session_id_completer  # type: ignore[attr-defined]  # argcomplete
    replay.set_defaults(handler=_replay)

    completion = commands.add_parser("completion", help="Print the shell completion script.")
    completion.add_argument("shell", choices=SHELLS)
    completion.set_defaults(handler=_completion)
    return parser


# -- doctor ----------------------------------------------------------------------------------


def _doctor(_args: argparse.Namespace, write: Callable[[str], None]) -> int:
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
    for tool in ("node", "npx"):
        path = shutil.which(tool)
        if path is None:
            check(_FAIL if settings.mcp_servers else _WARN, tool, "not on PATH (Playwright MCP needs Node.js >= 18)")
            continue
        check(_OK, tool, f"{_version(path, timeout)} · {path}")
    chrome = Path(settings.browser.chrome_path)
    check(_OK if chrome.is_file() else _FAIL, "chrome", chrome)
    port = settings.browser.cdp_port
    free = _port_free(port)
    check(_OK if free else _WARN, "cdp port", f"{port}" if free else f"{port} in use (a Chrome may already listen)")
    servers = ", ".join(f"{name} ({config.transport})" for name, config in settings.mcp_servers.items())
    check(_OK if servers else _WARN, "mcp servers", servers or "none configured")

    caps = detect(settings.ui)
    check(
        _OK if caps.level != "plain" else _WARN,
        "terminal",
        f"{caps.level} · {caps.host} · colors {caps.color} · unicode {caps.unicode} · {caps.reason}",
    )
    return 1 if failures else 0


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
