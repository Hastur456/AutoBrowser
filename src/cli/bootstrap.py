"""Bootstrap helpers that wire CLI arguments into a session runtime."""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from pathlib import Path
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TextIO

from src.providers.ollama import ollama_llm_factory

from src.agent_loop.execution.loop import HumanInputCallback
from src.cli.agent_cli import run_cli
from src.cli.approval import ApprovalPrompt
from src.cli.output import print_tools
from src.cli.sessions import ResumeError, resolve_resume
from src.cli.tasks import resolve_initial_task
from src.agent_loop.events import EventSink
from src.agent_loop.goals import GoalLimits
from src.config import UiSettings, get_settings
from src.harness.approvals import ApprovalBroker
from src.harness.chrome import start_chrome_cdp, wait_for_port
from src.harness.event_stream import EventBroadcaster
from src.harness.session import (
    MCPRuntimeFactory,
    SessionConfig,
    SessionRuntime,
    ToolPrinter,
    default_mcp_runtime_factory,
)


#: Exit code of a command line that cannot be acted on (argparse uses the same).
USAGE_EXIT_CODE = 2

#: The TUI input history under the storage root (``ui.input_history``).
INPUT_HISTORY_FILE = "input_history"


def is_interactive(args: argparse.Namespace) -> bool:
    """Can a human answer approval prompts? ``args.interactive`` (batch: ``False``) or a TTY."""

    explicit = getattr(args, "interactive", None)
    if explicit is not None:
        return bool(explicit)
    return sys.stdin.isatty()


def effective_permission_mode(args: argparse.Namespace, *, interactive: bool) -> str | None:
    """``--permission-mode`` wins; without a human a configured ``default`` becomes ``dont_ask``.

    ``None`` keeps ``settings.permissions.mode``. ``dont_ask`` turns approvals into
    non-terminal denies instead of ending every task that hits one.
    """

    explicit = getattr(args, "permission_mode", None)
    if explicit:
        return str(explicit)
    if not interactive and get_settings().permissions.mode == "default":
        return "dont_ask"
    return None


def build_session(
    args: argparse.Namespace,
    *,
    llm_factory: Callable[..., Any] = ollama_llm_factory,
    start_chrome: Callable[[str, str, int], Any] = start_chrome_cdp,
    wait_for_cdp_port: Callable[[int, float], Awaitable[None]] = wait_for_port,
    mcp_runtime_factory: MCPRuntimeFactory = default_mcp_runtime_factory,
    tool_printer: ToolPrinter | None = print_tools,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[..., None] = print,
    human_input: HumanInputCallback | None = None,
    event_sinks: Sequence[EventSink] = (),
    resume_from: Path | None = None,
) -> SessionRuntime:
    """Build a process-long session runtime from parsed CLI arguments.

    MCP servers come from ``settings.mcp_servers`` via ``mcp_runtime_factory``; they are
    started together with the session and shut down by ``SessionRuntime.close()``.
    ``human_input`` answers permission approvals; without it they are denied.
    """

    args = copy.copy(args)
    args.permission_mode = effective_permission_mode(args, interactive=is_interactive(args))
    session_config = SessionConfig.from_args(args)
    return SessionRuntime(
        session_config,
        llm_factory=llm_factory,
        start_chrome_cdp=start_chrome,
        wait_for_port=wait_for_cdp_port,
        mcp_runtime_factory=mcp_runtime_factory,
        print_tools=tool_printer,
        input_fn=input_fn,
        output_fn=output_fn,
        human_input=human_input,
        event_sinks=event_sinks,
        resume_from=resume_from,
    )


def ui_settings_from_args(args: argparse.Namespace) -> UiSettings:
    """``settings.ui`` with this run's ``--classic/--plain/--ascii/--no-color/--theme``."""

    update: dict[str, object] = {}
    if getattr(args, "classic", False):
        update["frontend"] = "classic"
    elif getattr(args, "plain", False):
        update["frontend"] = "plain"
    if getattr(args, "ascii", False):
        update["glyphs"] = "ascii"
    if getattr(args, "no_color", False):
        update["color"] = "none"
    if getattr(args, "theme", None):
        update["theme"] = args.theme
    ui = get_settings().ui
    return ui.model_copy(update=update) if update else ui


async def run_agent(args: argparse.Namespace) -> int:
    """Run the agent with parsed CLI arguments."""

    session = build_session(args)
    try:
        return await session.run_forever(initial_task=resolve_initial_task(args))
    finally:
        await session.close()


def run_agent_cli(args: argparse.Namespace) -> int:
    """Run the interactive front end: the terminal UI by default, plain without a capable
    terminal, the cmd2 shell with ``--classic`` (``ui.frontend=classic``).

    With a human at the terminal, approvals are asked in the front end; without one they are
    denied (``dont_ask``).
    """

    ui = ui_settings_from_args(args)
    try:
        resume_from = resolve_resume(args, interactive=is_interactive(args))
    except ResumeError as exc:
        print(f"autobrowser: {exc}", file=sys.stderr)
        return USAGE_EXIT_CODE
    if getattr(args, "print_mode", False):
        return run_print_cli(args, ui, resume_from=resume_from)
    if ui.frontend == "classic":
        return run_classic_cli(args, resume_from=resume_from)
    return run_frontend_cli(args, ui, resume_from=resume_from)


def run_print_cli(
    args: argparse.Namespace,
    ui: UiSettings,
    *,
    resume_from: Path | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """``-p``: one task, no front end, only the result on stdout (§7.6)."""

    from src.ui.controller import SessionController
    from src.ui.launch import OutputRelay
    from src.ui.logging_setup import terminal_quiet
    from src.ui.print_mode import INTERRUPTED_EXIT_CODE, run_print

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    _utf8_when_redirected(stdout)
    task = resolve_initial_task(args)
    if not task and not stdin.isatty():
        task = stdin.read().strip()
    if not task:
        print("autobrowser: -p needs a task (an argument, --task or stdin).", file=stderr)
        return USAGE_EXIT_CODE

    settings = get_settings()
    args = copy.copy(args)
    # Nobody answers approvals in print mode: ``dont_ask`` unless --permission-mode says.
    args.interactive = False
    broadcaster = EventBroadcaster(ui)
    session = build_session(
        args,
        human_input=None,
        event_sinks=(broadcaster,),
        output_fn=OutputRelay(),
        resume_from=resume_from,
    )
    controller = SessionController(session, broadcaster=broadcaster)

    def out(line: str) -> None:
        print(line, file=stdout, flush=True)

    def err(line: str) -> None:
        print(line, file=stderr, flush=True)

    async def main() -> int:
        try:
            return await run_print(
                controller,
                task,
                output_format=args.output_format or ui.print_format,
                verbose=bool(getattr(args, "verbose", False)),
                settings=ui,
                write_out=out,
                write_err=err,
            )
        finally:
            await controller.shutdown(ui.shutdown_timeout_seconds)
            await session.close()

    with terminal_quiet(ui.resolved_log_file(settings.storage)):
        try:
            return asyncio.run(main())
        except KeyboardInterrupt:
            err("[cancelled] interrupted")
            return INTERRUPTED_EXIT_CODE


def _utf8_when_redirected(stream: TextIO) -> None:
    """A pipe or file gets UTF-8, not the console code page (B7); a console keeps its own."""

    if not stream.isatty() and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def run_classic_cli(args: argparse.Namespace, *, resume_from: Path | None = None) -> int:
    """The cmd2 shell: approvals as ``y`` once, ``s`` for the session, ``n`` deny."""

    approvals = ApprovalPrompt() if is_interactive(args) else None
    session = build_session(args, human_input=approvals, resume_from=resume_from)
    return run_cli(session, initial_task=resolve_initial_task(args), approvals=approvals)


def run_frontend_cli(
    args: argparse.Namespace, ui: UiSettings, *, resume_from: Path | None = None
) -> int:
    """The terminal UI or plain mode over a :class:`SessionController`."""

    from src.ui.controller import SessionController
    from src.ui.launch import OutputRelay, run_frontend
    from src.ui.runtime_loop import RuntimeLoop

    settings = get_settings()
    broker = (
        ApprovalBroker(
            settings.permissions,
            progress_timeout_seconds=GoalLimits.from_settings(settings.loop).progress_timeout_seconds,
        )
        if is_interactive(args)
        else None
    )
    broadcaster = EventBroadcaster(ui)
    relay = OutputRelay()
    session = build_session(
        args,
        human_input=broker,
        event_sinks=(broadcaster,),
        output_fn=relay,
        resume_from=resume_from,
    )
    controller = SessionController(session, broker=broker, broadcaster=broadcaster)
    runtime = RuntimeLoop()
    try:
        return run_frontend(
            controller,
            runtime,
            settings=ui,
            log_file=ui.resolved_log_file(settings.storage),
            initial_task=resolve_initial_task(args),
            history_path=settings.storage.root_dir / INPUT_HISTORY_FILE,
            output=relay,
        )
    finally:
        # Never close the session under a running start or task (MCP/Chrome would leak).
        runtime.run(controller.shutdown(ui.shutdown_timeout_seconds))
        runtime.run(session.close())
        runtime.stop()


__all__ = [
    "build_session",
    "effective_permission_mode",
    "is_interactive",
    "run_agent",
    "run_agent_cli",
    "run_classic_cli",
    "run_print_cli",
    "run_frontend_cli",
    "ui_settings_from_args",
]
