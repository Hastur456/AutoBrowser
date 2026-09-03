"""cmd2-based interactive CLI for the AutoBrowser session runtime."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import cmd2
from cmd2 import Cmd2ArgumentParser, with_argparser

from src.harness.session import SessionContext, SessionRuntime, TaskRecord
from src.harness.tools import ToolRegistry


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
    CommandSpec("Browser", "snapshot", "snapshot", "Save a screenshot to workspace/screenshots."),
    CommandSpec("Browser", "url", "url", "Show the current page URL."),
    CommandSpec("Service", "help", "help [command]", "Show command help."),
    CommandSpec("Service", "exit", "exit / quit", "Exit the CLI."),
    CommandSpec("Service", "status", "status", "Show short session and browser status."),
)


run_parser = Cmd2ArgumentParser(description="Run a new browser-agent task.")
run_parser.add_argument("text", nargs=argparse.REMAINDER, help="Task text.")

history_parser = Cmd2ArgumentParser(description="Show dialogue history.")
history_parser.add_argument("limit", nargs="?", type=int, default=10, help="Number of messages.")


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
            self._thread.join(timeout=5)

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

    def __init__(self, runtime: SessionRuntime, *, use_color: bool | None = None) -> None:
        super().__init__(allow_cli_args=False, include_ipy=False)
        self.prompt = "autobrowser> "
        self.runtime = runtime
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
        """Treat free-form input as an agent task."""

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

        self._runtime_loop.run(self._reset_session())
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
        browser_state = "disabled" if self.runtime.config.no_mcp else "available"
        if not context.initialized:
            browser_state = "not started" if not self.runtime.config.no_mcp else "disabled"
        self.poutput(f"Browser: {browser_state}")
        self.poutput(f"Tools:   {self._tool_count_text(context.tool_registry)}")

    def do_snapshot(self, _: str) -> None:
        """Save a screenshot of the current page to workspace/screenshots."""

        try:
            path = self._runtime_loop.run(self._take_snapshot())
        except Exception as exc:
            self.perror(f"Snapshot failed: {exc}")
            return
        self.poutput(self._ok(f"Saved screenshot: {path}"))

    def do_url(self, _: str) -> None:
        """Show the current page URL."""

        try:
            url = self._runtime_loop.run(self._current_url())
        except Exception as exc:
            self.perror(f"URL lookup failed: {exc}")
            return
        self.poutput(url or "Current URL is unavailable.")

    def do_status(self, _: str) -> None:
        """Show short session, browser, and task status."""

        context = self.runtime.context
        browser = "off" if self.runtime.config.no_mcp else ("on" if context.initialized else "idle")
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

    async def _reset_session(self) -> None:
        await self.runtime.close()
        self.runtime.context = SessionContext(self.runtime.config)

    async def _take_snapshot(self) -> Path:
        await self.runtime.start()
        workspace = self.runtime.context.workspace
        if workspace is None:
            raise RuntimeError("Session workspace is not initialized.")

        path = workspace.screenshots / f"snapshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        tool = await self._get_tool("browser_take_screenshot", "browser_screenshot")
        await self._call_tool(tool, {"filename": str(path)})

        if not path.exists():
            result = await self._call_tool(tool, {})
            await self._write_tool_image_result(path, result)

        self.runtime.context.artifacts.register("snapshot", path, kind="screenshot")
        self.runtime.context.persist()
        return path

    async def _current_url(self) -> str | None:
        await self.runtime.start()
        evaluate = await self._get_optional_tool("browser_evaluate")
        if evaluate is not None:
            result = await self._call_tool(
                evaluate,
                {"function": "() => window.location.href"},
            )
            text = self._result_text(result).strip()
            if text:
                return text.strip('"')

        snapshot = await self._get_optional_tool("browser_snapshot")
        if snapshot is None:
            return None
        text = self._result_text(await self._call_tool(snapshot, {}))
        return self._extract_url_from_snapshot(text)

    async def _get_tool(self, *names: str) -> Any:
        tool = await self._get_optional_tool(*names)
        if tool is None:
            raise RuntimeError(f"Required browser tool is unavailable: {'/'.join(names)}")
        return tool

    async def _get_optional_tool(self, *names: str) -> Any | None:
        registry = self.runtime.context.tool_registry
        if registry is None:
            return None
        tools = await registry.get_by_name()
        for name in names:
            if name in tools:
                return tools[name]
        return None

    async def _call_tool(self, tool: Any, payload: dict[str, Any]) -> Any:
        invoke = getattr(tool, "invoke", None)
        if callable(invoke):
            result = invoke(payload)
            if inspect.isawaitable(result):
                return await result
            return result

        if callable(tool):
            result = tool(**payload)
            if inspect.isawaitable(result):
                return await result
            return result

        raise TypeError(f"Tool {tool!r} is not invocable.")

        raise TypeError(f"Unsupported tool type: {type(tool).__name__}")

    async def _write_tool_image_result(self, path: Path, result: Any) -> None:
        data = getattr(result, "data", None)
        if isinstance(data, bytes):
            path.write_bytes(data)
            return
        if isinstance(result, bytes):
            path.write_bytes(result)
            return
        raise RuntimeError("Screenshot tool did not create a file or return image bytes.")

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
            self._task_done.wait(timeout=5)
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
            self._task_done.wait(timeout=5)
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
        if isinstance(record.result, BaseException):
            return self._style("failed", "red")
        return self._style("done", "green")

    def _tool_count_text(self, registry: ToolRegistry | None) -> str:
        if registry is None:
            return "0"
        try:
            tools = self._runtime_loop.run(registry.get_all())
        except Exception:
            return "unavailable"
        return str(len(tools))

    def _result_text(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        content = getattr(result, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, Iterable):
            parts: list[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
            if parts:
                return "\n".join(parts)
        return str(result)

    def _extract_url_from_snapshot(self, text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(("url:", "page url:")):
                return stripped.split(":", 1)[1].strip()
            if stripped.startswith(("http://", "https://")):
                return stripped
        return None

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


def run_cli(runtime: SessionRuntime, *, initial_task: str | None = None) -> int:
    """Run the interactive CLI for a prepared `SessionRuntime`."""

    cli = AgentCli(runtime)
    try:
        if initial_task:
            cli._runtime_loop.run(runtime.run_task(initial_task))
        cli.cmdloop()
        return 0
    finally:
        cli.close_runtime()


__all__ = ["AgentCli", "CommandSpec", "run_cli"]
