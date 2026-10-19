"""cmd2-based interactive CLI for the AutoBrowser session runtime."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cmd2
from cmd2 import Cmd2ArgumentParser, with_argparser

from src.browser.pages import current_page_url
from src.cli.output import format_mcp_status
from src.cli.approval import ApprovalPrompt
from src.config import get_settings
from src.harness.session import SessionRuntime, TaskRecord
from src.harness.tools import ToolRegistry
from src.mcp import ConnectionState

@dataclass(frozen=True)
class CommandSpec:
    """Small command catalog entry used by the grouped help overview."""

    group: str
    name: str
    usage: str
    description: str


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("Tasks", "run", "run <text>", "Run a new browser-agent task."),
    CommandSpec("Tasks", "tasks", "tasks", "Show all tasks in this session."),
    CommandSpec("Tasks", "cancel", "cancel", "Cancel the currently running task."),
    CommandSpec("Session", "session", "session", "Show current session information."),
    CommandSpec("Session", "reset", "reset", "Reset session history and state."),
    CommandSpec("Memory", "history", "history [N]", "Show the last N dialogue messages."),
    CommandSpec("Memory", "clear", "clear", "Clear dialogue history."),
    CommandSpec("Browser", "browser", "browser", "Show browser/tool status."),
    CommandSpec("Browser", "url", "url", "Show the page URL the agent last saw."),
    CommandSpec("MCP", "mcp", "mcp [reconnect N]", "Show MCP servers or restart server N."),
    CommandSpec("Service", "help", "help [command]", "Show command help."),
    CommandSpec("Service", "exit", "exit / quit", "Exit the CLI."),
    CommandSpec("Service", "status", "status", "Show short session and browser status."),
)


run_parser = Cmd2ArgumentParser(description="Run a new browser-agent task.")
run_parser.add_argument("text", nargs=argparse.REMAINDER, help="Task text.")

history_parser = Cmd2ArgumentParser(description="Show dialogue history.")
history_parser.add_argument("limit", nargs="?", type=int, default=10, help="Number of messages.")

mcp_parser = Cmd2ArgumentParser(description="Show MCP server status or restart a server.")
mcp_parser.add_argument("action", nargs="?", choices=("status", "reconnect"), default="status")
mcp_parser.add_argument("server", nargs="?", help="Server name (for reconnect).")


class RuntimeLoop:
    """Own one event loop for all async session operations."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="autobrowser-runtime-loop",
            daemon=True,
        )
        self._thread.start()
        self._started.wait()

    def submit(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Any) -> Any:
        return self.submit(coro).result()

    def stop(self) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=get_settings().ui.shutdown_timeout_seconds)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()


class AgentCli(cmd2.Cmd):
    """Interactive shell around an initialized or lazy `SessionRuntime`."""

    prompt = "autobrowser> "
    intro = "AutoBrowser CLI. Type 'help' for commands."

    def __init__(
        self,
        runtime: SessionRuntime,
        *,
        use_color: bool | None = None,
        approvals: ApprovalPrompt | None = None,
    ) -> None:
        super().__init__(allow_cli_args=False, include_ipy=False)
        self.prompt = "autobrowser> "
        self.runtime = runtime
        #: Pending permission approvals of the running task; the next input line answers.
        self.approvals = approvals
        self._runtime_loop = RuntimeLoop()
        self.use_color = sys.stdout.isatty() if use_color is None else use_color
        self._task_future: Future[Any] | None = None
        self._task_done = threading.Event()
        self._task_done.set()
        self._task_error: BaseException | None = None
        self._task_result: Any | None = None
        self._task_text: str | None = None
        self._closed = False
        self._disable_extra_cmd2_commands()

    def _disable_extra_cmd2_commands(self) -> None:
        for command in (
            "alias",
            "edit",
            "macro",
            "run_pyscript",
            "run_script",
            "set",
            "shell",
            "shortcuts",
        ):
            self.disable_command(command, "This CLI exposes only AutoBrowser commands.")

    def default(self, statement: cmd2.Statement) -> None:
        """Treat free-form input as an agent task (or the answer to a pending approval)."""

        if self.approvals is not None and self.approvals.pending:
            self.approvals.answer(statement.raw)
            return
        text = str(statement).strip()
        if text:
            self._run_task_text(text)

    def do_help(self, arg: str) -> None:
        """Show grouped command help, or detailed help for one command."""

        command = arg.strip()
        if command:
            super().do_help(command)
            return

        current_group = ""
        for spec in COMMANDS:
            if spec.group != current_group:
                current_group = spec.group
                self.poutput(self._style(current_group, "cyan"))
            self.poutput(f"  {spec.usage:<18} {spec.description}")

    @with_argparser(run_parser)
    def do_run(self, args: argparse.Namespace) -> None:
        """Run a new browser-agent task: run <text>."""

        task_text = " ".join(args.text).strip()
        self._run_task_text(task_text)

    def _run_task_text(self, task_text: str) -> None:
        if not task_text:
            self.perror("Task text is required.")
            return
        if self._is_task_running():
            self.perror("A task is already running. Use 'cancel' first.")
            return

        self._start_background_task(task_text)
        self.poutput(self._ok(f"Task started: {task_text}"))

    def do_tasks(self, _: str) -> None:
        """Show all tasks in the current session."""

        tasks = self.runtime.context.tasks
        if not tasks:
            self.poutput("No tasks in this session.")
            return

        for index, record in enumerate(tasks, start=1):
            status = self._task_status(record)
            started = self._format_dt(record.started_at)
            self.poutput(f"{index:>2}. {status:<9} {started} {record.task}")

    def do_cancel(self, _: str) -> None:
        """Cancel the currently running task."""

        if not self._is_task_running():
            self.poutput("No task is currently running.")
            return

        future = self._task_future
        if future is None:
            self.perror("Running task cannot be cancelled yet.")
            return

        future.cancel()
        self.poutput(self._warn("Cancellation requested."))

    def do_session(self, _: str) -> None:
        """Show current session information."""

        context = self.runtime.context
        status = "running" if self._is_task_running() else "ready"
        if not context.initialized:
            status = "not started"
        self.poutput(f"Session ID: {context.session_id}")
        self.poutput(f"Status:     {status}")
        self.poutput(f"Tasks:      {len(context.tasks)}")
        self.poutput(f"Started:    {self._format_dt(context.metadata.started_at)}")
        self.poutput(f"Workspace:  {context.workspace.root if context.workspace else '-'}")

    def do_reset(self, _: str) -> None:
        """Reset the current session by clearing history and state."""

        if self._is_task_running():
            self.perror("Cannot reset while a task is running. Use 'cancel' first.")
            return

        self._runtime_loop.run(self.runtime.reset())
        self.poutput(self._ok("Session reset."))

    @with_argparser(history_parser)
    def do_history(self, args: argparse.Namespace) -> None:
        """Show the last N dialogue messages."""

        messages = self._get_history_messages()
        if not messages:
            self.poutput("No dialogue history is available.")
            return

        limit = max(args.limit, 0)
        for message in messages[-limit:]:
            self.poutput(self._format_message(message))

    def do_clear(self, _: str) -> None:
        """Clear dialogue history."""

        count = self._clear_history_messages()
        self.runtime.context.persist()
        self.poutput(self._ok(f"Dialogue history cleared ({count} item(s))."))

    def do_browser(self, _: str) -> None:
        """Show browser status."""

        context = self.runtime.context
        self.poutput(f"Browser: {self._browser_state()}")
        mcp = context.mcp
        if mcp is not None and mcp.browser_server:
            self.poutput(f"Server:  {mcp.browser_server}")
        self.poutput(f"Tools:   {self._tool_count_text(context.tool_registry)}")

    @with_argparser(mcp_parser)
    def do_mcp(self, args: argparse.Namespace) -> None:
        """Show MCP server status, or restart one server: mcp reconnect <name>."""

        mcp = self.runtime.context.mcp
        if mcp is None:
            self.poutput("MCP is disabled." if self.runtime.config.no_mcp else "MCP is not started.")
            return
        if args.action == "reconnect":
            name = args.server or mcp.browser_server
            if not name:
                self.perror("Server name is required.")
                return
            if self._is_task_running():
                self.perror("Cannot reconnect while a task is running. Use 'cancel' first.")
                return
            try:
                self._runtime_loop.run(mcp.manager.reconnect(name))
            except Exception as exc:
                self.perror(f"Reconnect failed: {exc}")
                return
            self.poutput(self._ok(f"Reconnected {name} (generation {mcp.manager.generation(name)})."))
            return
        for line in format_mcp_status(mcp.status()):
            self.poutput(line)

    def do_url(self, _: str) -> None:
        """Show the page URL the agent last saw (no tool is called)."""

        url = current_page_url(dict(self.runtime.context.state))
        self.poutput(url or "Current URL is unavailable.")

    def do_status(self, _: str) -> None:
        """Show short session, browser, and task status."""

        context = self.runtime.context
        browser = self._browser_state()
        task = "running" if self._is_task_running() else "idle"
        self.poutput(
            f"session={context.session_id[:8]} status={task} "
            f"browser={browser} tasks={len(context.tasks)}"
        )

    def do_exit(self, _: str) -> bool:
        """Exit the CLI."""

        return self._exit()

    def do_quit(self, _: str) -> bool:
        """Exit the CLI."""

        return self._exit()

    def _start_background_task(self, task_text: str) -> None:
        self._task_done.clear()
        self._task_error = None
        self._task_result = None
        self._task_text = task_text

        self._task_future = self._runtime_loop.submit(self.runtime.run_task(task_text))
        self._task_future.add_done_callback(self._on_task_done)

    def _on_task_done(self, future: Future[Any]) -> None:
        try:
            self._task_result = future.result()
            answer = getattr(self._task_result, "final_answer", None)
            if answer:
                self.poutput(str(answer))
            self.poutput(self._ok("Task finished."))
        except BaseException as exc:
            if future.cancelled() or isinstance(exc, asyncio.CancelledError):
                self.poutput(self._warn("Task cancelled."))
            else:
                self._task_error = exc
                self.perror(f"Task failed: {exc}")
        finally:
            self._task_future = None
            self._task_text = None
            self._task_done.set()

    def _is_task_running(self) -> bool:
        return not self._task_done.is_set()

    def _exit(self) -> bool:
        if self._is_task_running():
            self.do_cancel("")
            self.poutput("Waiting for running task to stop...")
            self._task_done.wait(timeout=get_settings().ui.shutdown_timeout_seconds)
        self.close_runtime()
        self.poutput("Bye.")
        return True

    def close_runtime(self) -> None:
        """Close the session runtime and its dedicated event loop once."""

        if self._closed:
            return
        self._closed = True
        if self._is_task_running():
            future = self._task_future
            if future is not None:
                future.cancel()
            self._task_done.wait(timeout=get_settings().ui.shutdown_timeout_seconds)
        self._runtime_loop.run(self.runtime.close())
        self._runtime_loop.stop()

    def _get_history_messages(self) -> list[Any]:
        state = self.runtime.context.state
        for key in ("messages", "history", "conversation", "conversation_history"):
            value = state.get(key)
            if isinstance(value, list):
                return value
        return []

    def _clear_history_messages(self) -> int:
        state = self.runtime.context.state
        count = 0
        for key in ("messages", "history", "conversation", "conversation_history"):
            value = state.get(key)
            if isinstance(value, list):
                count += len(value)
                state[key] = []
        return count

    def _format_message(self, message: Any) -> str:
        role = getattr(message, "type", None) or getattr(message, "role", None) or type(message).__name__
        content = getattr(message, "content", message)
        return f"{self._style(str(role), 'cyan')}: {content}"

    def _task_status(self, record: TaskRecord) -> str:
        if record.finished_at is None:
            return self._style("running", "yellow")
        if isinstance(record.result, asyncio.CancelledError):
            return self._style("cancelled", "yellow")
        if isinstance(record.result, BaseException):
            return self._style("failed", "red")
        return self._style("done", "green")

    def _browser_state(self) -> str:
        if self.runtime.config.no_mcp:
            return "disabled"
        context = self.runtime.context
        mcp = context.mcp
        if not context.initialized or mcp is None:
            return "not started"
        if not mcp.browser_server:
            return "no browser server"
        status = mcp.status().get(mcp.browser_server)
        if status is None:
            return "unknown"
        if status.state is ConnectionState.READY:
            return "on"
        return status.state.value

    def _tool_count_text(self, registry: ToolRegistry | None) -> str:
        if registry is None:
            return "0"
        try:
            tools = self._runtime_loop.run(registry.get_all())
        except Exception:
            return "unavailable"
        return str(len(tools))

    def _format_dt(self, value: datetime | None) -> str:
        return value.isoformat(timespec="seconds") if value else "-"

    def _ok(self, text: str) -> str:
        return self._style(text, "green")

    def _warn(self, text: str) -> str:
        return self._style(text, "yellow")

    def _style(self, text: str, color: str) -> str:
        if not self.use_color:
            return text
        colors = {
            "cyan": "\033[36m",
            "green": "\033[32m",
            "red": "\033[31m",
            "yellow": "\033[33m",
        }
        return f"{colors[color]}{text}\033[0m"


def _run_blocking(cli: AgentCli, coro: Any) -> Any:
    """Run ``coro`` on the runtime loop, answering approvals on this (terminal) thread."""

    future = cli._runtime_loop.submit(coro)
    while True:
        try:
            return future.result(timeout=0.1)
        except FutureTimeoutError:
            if cli.approvals is not None:
                cli.approvals.serve()


def run_cli(
    runtime: SessionRuntime,
    *,
    initial_task: str | None = None,
    approvals: ApprovalPrompt | None = None,
) -> int:
    """Run the interactive CLI for a prepared `SessionRuntime`."""

    cli = AgentCli(runtime, approvals=approvals)
    try:
        if initial_task:
            _run_blocking(cli, runtime.run_task(initial_task))
        cli.cmdloop()
        return 0
    finally:
        cli.close_runtime()


__all__ = ["AgentCli", "CommandSpec", "run_cli"]
