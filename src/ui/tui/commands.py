"""Slash commands of the terminal UI and their completion (§5.6).

A command is a coroutine run on the UI loop; it reaches the session only through the
:class:`CommandHost` (whose controller calls run on the runtime loop) and answers with a
:class:`CommandResult`: messages for the render pump plus app-level effects (exit, theme,
clear). Argument completion reads the host's last cached views, never the session itself, so
typing never blocks on the runtime thread.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from src.contracts import MemoryEntry
from src.ui.feed import sanitize_text
from src.ui.tui.strings import Strings
from src.ui.tui.ports import ControllerPort, RuntimeBridge
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.transcript import (
    CommandOutput,
    KeyAction,
    Notice,
    PickerOption,
    PickerShown,
    StreamReset,
    Transcript,
    UiMessage,
)
from src.ui.views import SessionView, StoredSessionView, TaskView

#: ``picker_id`` of the ``/resume`` list.
RESUME_PICKER = "resume"

THEMES = ("dark", "light", "mono")
DETAILS = ("compact", "verbose")


@dataclass(frozen=True)
class CommandResult:
    messages: tuple[UiMessage, ...] = ()
    exit: bool = False
    clear: bool = False
    theme: str | None = None
    #: ``/resume <target>``: the front end switches to that stored session.
    resume: str | None = None
    #: The stored sessions changed (``/reset`` stored the previous one): refresh caches.
    refresh_sessions: bool = False


class CommandHost(Protocol):
    """What a command may use: the controller (via the bridge) and the app's state."""

    @property
    def controller(self) -> ControllerPort: ...
    @property
    def bridge(self) -> RuntimeBridge: ...
    @property
    def caps(self) -> TerminalCaps: ...
    @property
    def transcript(self) -> Transcript: ...
    @property
    def view(self) -> SessionView | None: ...
    @property
    def locale(self) -> str: ...
    @property
    def stored_ids(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    summary: dict[str, str]
    run: Callable[[CommandHost, str], Awaitable[CommandResult]]
    complete_args: Callable[[CommandHost, str], Sequence[str]] | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def describe(self, locale: str) -> str:
        return self.summary.get(locale) or self.summary["en"]


# ---------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------


async def _help(host: CommandHost, _args: str) -> CommandResult:
    width = max(len(command.usage) for command in COMMANDS)
    lines = tuple(f"{command.usage:<{width}}  {command.describe(host.locale)}" for command in COMMANDS)
    keys = {
        "ru": "Esc — прервать · Ctrl+C ×2 — выход · Ctrl+O — подробности · Ctrl+T — план",
        "en": "Esc interrupt · Ctrl+C x2 exit · Ctrl+O details · Ctrl+T plan",
    }
    title = "Команды" if host.locale == "ru" else "Commands"
    return CommandResult(messages=(CommandOutput(title, (*lines, "", keys[host.locale])),))


async def _exit(_host: CommandHost, _args: str) -> CommandResult:
    return CommandResult(exit=True)


async def _clear(_host: CommandHost, _args: str) -> CommandResult:
    return CommandResult(clear=True)


async def _detail(host: CommandHost, args: str) -> CommandResult:
    wanted = args.strip()
    current = host.transcript.detail
    if not wanted:
        wanted = "verbose" if current == "compact" else "compact"
    if wanted not in DETAILS:
        return _usage_error(host, "/detail compact|verbose")
    if wanted == current:
        return CommandResult()
    return CommandResult(messages=(KeyAction("toggle_detail"),))


async def _theme(host: CommandHost, args: str) -> CommandResult:
    wanted = args.strip()
    if wanted not in THEMES:
        return _usage_error(host, "/theme dark|light|mono")
    return CommandResult(theme=wanted)


async def _status(host: CommandHost, _args: str) -> CommandResult:
    view = await host.bridge.call(host.controller.session_view)
    ru = host.locale == "ru"
    rows = (
        f"{'сессия' if ru else 'session':<12} {view.session_id}",
        f"{'модель' if ru else 'model':<12} {' · '.join(p for p in (view.model, view.provider) if p)}",
        f"{'каталог' if ru else 'workspace':<12} {view.workspace}",
        f"{'разрешения' if ru else 'permissions':<12} {view.permission_mode}",
        f"{'хуки' if ru else 'hooks':<12} {_on_off(view.hooks_enabled, ru)}",
        f"{'память' if ru else 'memory':<12} {_on_off(view.memory_enabled, ru)}",
        f"{'задача' if ru else 'task':<12} {view.current_task_id or '-'}",
    )
    return CommandResult(messages=(CommandOutput("Статус" if ru else "Status", rows),))


async def _tasks(host: CommandHost, _args: str) -> CommandResult:
    tasks = await host.bridge.call(host.controller.task_views)
    view = host.view
    ru = host.locale == "ru"
    title = f"{'Задачи сессии' if ru else 'Session tasks'} {view.session_id[:8] if view else ''}".rstrip()
    if not tasks:
        return CommandResult(messages=(CommandOutput(title, ("нет задач" if ru else "no tasks",)),))
    header = f"{'#':>3}  {'статус' if ru else 'status':<10} {'время' if ru else 'time':>6} {'ходы' if ru else 'turns':>5}  {'задача' if ru else 'task'}"
    rows = [header, *(_task_row(task) for task in tasks)]
    return CommandResult(messages=(CommandOutput(title, tuple(rows)),))


async def _cancel(host: CommandHost, _args: str) -> CommandResult:
    cancelled = await host.bridge.run(host.controller.cancel)
    if cancelled:
        return CommandResult()
    text = "нет выполняемой задачи" if host.locale == "ru" else "no task is running"
    return CommandResult(messages=(Notice(text),))


async def _mcp(host: CommandHost, args: str) -> CommandResult:
    parts = args.split()
    ru = host.locale == "ru"
    if parts and parts[0] == "reconnect":
        if len(parts) != 2:
            return _usage_error(host, "/mcp reconnect <name>")
        name = parts[1]
        server = await host.bridge.run(lambda: host.controller.reconnect(name))
        return CommandResult(messages=(Notice(f"{server.name}: {server.state}"),))
    if parts:
        return _usage_error(host, "/mcp [reconnect <name>]")
    view = await host.bridge.call(host.controller.session_view)
    title = "MCP-серверы" if ru else "MCP servers"
    if not view.servers:
        return CommandResult(messages=(CommandOutput(title, (view.browser or "-",)),))
    rows = []
    for server in view.servers:
        tools = f"{server.tool_count} {'инстр.' if ru else 'tools'}"
        extra = server.error or ("stateful" if server.stateful else "")
        rows.append(
            f"{server.name:<14} {server.state:<9} gen {server.generation:<3} "
            f"{server.transport:<7} {tools:>10}  {extra}".rstrip()
        )
    rows.append("/mcp reconnect <имя> — перезапустить сервер" if ru else "/mcp reconnect <name> — restart a server")
    return CommandResult(messages=(CommandOutput(title, tuple(rows)),))


async def _history(host: CommandHost, args: str) -> CommandResult:
    raw = args.strip() or "10"
    if not raw.isdigit():
        return _usage_error(host, "/history [N]")
    limit = int(raw)
    messages = await host.bridge.call(lambda: host.controller.history(limit))
    rows = []
    for message in messages:
        role = sanitize_text(message.get("role") or message.get("type") or "?")
        content = sanitize_text(message.get("content") or "").replace("\n", " ")
        rows.append(f"{role}: {content}")
    title = "История" if host.locale == "ru" else "History"
    return CommandResult(messages=(CommandOutput(title, tuple(rows) or ("-",)),))


async def _memory(host: CommandHost, _args: str) -> CommandResult:
    entries = await host.bridge.call(host.controller.memory_entries)
    ru = host.locale == "ru"
    title = "Память" if ru else "Memory"
    rows = tuple(sanitize_text(_entry_label(entry)) for entry in entries)
    return CommandResult(messages=(CommandOutput(title, rows or (("память пуста" if ru else "memory is empty"),)),))


async def _reset(host: CommandHost, _args: str) -> CommandResult:
    await host.bridge.run(host.controller.reset)
    text = "контекст сессии сброшен" if host.locale == "ru" else "session context reset"
    return CommandResult(messages=(StreamReset(), Notice(text)), refresh_sessions=True)


async def _url(host: CommandHost, _args: str) -> CommandResult:
    url = sanitize_text(await host.bridge.call(host.controller.current_url))
    if not url:
        text = "адрес страницы пока неизвестен" if host.locale == "ru" else "the page URL is not known yet"
        return CommandResult(messages=(Notice(text),))
    return CommandResult(messages=(CommandOutput("URL", (url,)),))


async def _replay(host: CommandHost, args: str) -> CommandResult:
    target = args.strip()
    try:
        lines = await host.bridge.run(lambda: host.controller.replay(target))
    except (LookupError, FileNotFoundError) as exc:
        return CommandResult(messages=(Notice(str(exc).strip("'\""), "warn"),))
    title = "Ход задачи" if host.locale == "ru" else "Replay"
    return CommandResult(messages=(CommandOutput(title, tuple(sanitize_text(line) for line in lines)),))


def _replay_targets(host: CommandHost, prefix: str) -> Sequence[str]:
    """Task numbers of this session (from the cached view)."""

    count = host.view.task_count if host.view is not None else 0
    return [str(n) for n in range(1, count + 1) if str(n).startswith(prefix.strip())]


async def _resume(host: CommandHost, args: str) -> CommandResult:
    """``/resume <№|id>`` switches at once; ``/resume`` opens the list of stored sessions."""

    target = args.strip()
    if target:
        return CommandResult(resume=target)
    sessions = await host.bridge.run(host.controller.stored_sessions)
    strings = Strings(host.locale)  # type: ignore[arg-type]
    if not sessions:
        return CommandResult(messages=(Notice(strings("resume_none")),))
    return CommandResult(messages=(resume_picker(sessions, strings),))


def resume_picker(sessions: Sequence[StoredSessionView], strings: Strings, *, now: datetime | None = None) -> PickerShown:
    """The ``/resume`` list: id, age, task count, and the last task to recognise it by."""

    moment = now or datetime.now(UTC)
    options = []
    for session in sessions:
        age = (
            strings.ago((moment - session.last_activity).total_seconds())
            if session.last_activity is not None
            else "-"
        )
        label = f"{session.session_id[:8]} · {age} · {strings.count('tasks', session.task_count)}"
        detail = sanitize_text(session.last_task).replace("\n", " ")
        options.append(PickerOption(value=session.session_id, label=label, detail=detail))
    return PickerShown(picker_id=RESUME_PICKER, title=strings("resume_title"), options=tuple(options))


def _session_ids(host: CommandHost, prefix: str) -> Sequence[str]:
    """Stored session ids for ``/resume <id>``, from the front end's cached list (never
    read from disk on a keystroke)."""

    word = prefix.strip()
    return [session_id for session_id in host.stored_ids if session_id.startswith(word)]


async def _permissions(host: CommandHost, _args: str) -> CommandResult:
    view = await host.bridge.call(host.controller.session_view)
    ru = host.locale == "ru"
    rows = (
        f"{'режим' if ru else 'mode':<8} {view.permission_mode}",
        "правила — permissions.rules в config.yaml" if ru else "rules — permissions.rules in config.yaml",
    )
    return CommandResult(messages=(CommandOutput("Разрешения" if ru else "Permissions", rows),))


async def _doctor(host: CommandHost, _args: str) -> CommandResult:
    caps = host.caps
    ru = host.locale == "ru"
    rows = (
        f"{'терминал' if ru else 'terminal':<10} {caps.host or '-'} · {caps.reason}",
        f"{'уровень' if ru else 'level':<10} {caps.level}",
        f"{'цвета' if ru else 'colors':<10} {caps.color}",
        f"{'unicode':<10} {_on_off(caps.unicode, ru)}",
        f"{'DEC 2026':<10} {_on_off(caps.sync_output, ru)}",
        f"{'размер' if ru else 'size':<10} {caps.width}x{caps.height}",
    )
    if caps.host == "vscode":
        hint = (
            "VS Code: при сдвигах ширины задайте terminal.integrated.unicodeVersion = 11"
            if ru
            else "VS Code: set terminal.integrated.unicodeVersion to 11 if widths look off"
        )
        rows = (*rows, hint)
    return CommandResult(messages=(CommandOutput("Диагностика" if ru else "Doctor", rows),))


def _usage_error(host: CommandHost, usage: str) -> CommandResult:
    prefix = "использование" if host.locale == "ru" else "usage"
    return CommandResult(messages=(Notice(f"{prefix}: {usage}", "warn"),))


def _on_off(value: bool, ru: bool) -> str:
    if ru:
        return "вкл." if value else "выкл."
    return "on" if value else "off"


def _task_row(task: TaskView) -> str:
    elapsed = ""
    if task.started_at is not None:
        end = task.finished_at or datetime.now(task.started_at.tzinfo)
        seconds = max(int((end - task.started_at).total_seconds()), 0)
        elapsed = f"{seconds // 60}:{seconds % 60:02d}"
    turns = "" if task.turns is None else str(task.turns)
    text = sanitize_text(task.task).replace("\n", " ")
    return f"{task.index:>3}  {task.status:<10} {elapsed:>6} {turns:>5}  {text}"


def _entry_label(entry: MemoryEntry) -> str:
    label = f"{entry.path} ({entry.status})"
    return f"{label} — {entry.description}" if entry.description else label


def _server_names(host: CommandHost, prefix: str) -> Sequence[str]:
    words = prefix.split()
    if not words or (len(words) == 1 and not prefix.endswith(" ")):
        return [w for w in ("reconnect",) if w.startswith(words[0] if words else "")]
    if words[0] != "reconnect":
        return []
    names = [server.name for server in (host.view.servers if host.view else ())]
    start = words[1] if len(words) > 1 else ""
    return [name for name in names if name.startswith(start)]


def _choices(values: Iterable[str]) -> Callable[[CommandHost, str], Sequence[str]]:
    def complete(_host: CommandHost, prefix: str) -> Sequence[str]:
        return [value for value in values if value.startswith(prefix.strip())]

    return complete


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "/help", {"ru": "список команд и клавиш", "en": "commands and keys"}, _help),
    SlashCommand("status", "/status", {"ru": "состояние сессии", "en": "session status"}, _status),
    SlashCommand("tasks", "/tasks", {"ru": "задачи сессии", "en": "tasks of the session"}, _tasks),
    SlashCommand("cancel", "/cancel", {"ru": "прервать задачу", "en": "interrupt the task"}, _cancel),
    SlashCommand(
        "mcp",
        "/mcp [reconnect <имя>]",
        {"ru": "MCP-серверы", "en": "MCP servers"},
        _mcp,
        complete_args=_server_names,
    ),
    SlashCommand("history", "/history [N]", {"ru": "история диалога", "en": "dialogue history"}, _history),
    SlashCommand("memory", "/memory", {"ru": "записи памяти", "en": "memory entries"}, _memory),
    SlashCommand("url", "/url", {"ru": "адрес текущей страницы", "en": "current page URL"}, _url),
    SlashCommand(
        "replay",
        "/replay [№ | id сессии]",
        {"ru": "ход задачи (последней, №, другой сессии)", "en": "action sequence of a task or session"},
        _replay,
        complete_args=_replay_targets,
    ),
    SlashCommand("permissions", "/permissions", {"ru": "режим разрешений", "en": "permission mode"}, _permissions),
    SlashCommand(
        "detail",
        "/detail compact|verbose",
        {"ru": "детализация вызовов", "en": "call detail"},
        _detail,
        complete_args=_choices(DETAILS),
    ),
    SlashCommand(
        "theme",
        "/theme dark|light|mono",
        {"ru": "тема", "en": "colour theme"},
        _theme,
        complete_args=_choices(THEMES),
    ),
    SlashCommand("doctor", "/doctor", {"ru": "возможности терминала", "en": "terminal capabilities"}, _doctor),
    SlashCommand(
        "resume",
        "/resume [№ | id]",
        {"ru": "продолжить сохранённую сессию", "en": "resume a stored session"},
        _resume,
        complete_args=_session_ids,
    ),
    SlashCommand("reset", "/reset", {"ru": "сбросить контекст сессии", "en": "reset the session context"}, _reset),
    SlashCommand("clear", "/clear", {"ru": "очистить экран", "en": "clear the screen"}, _clear),
    SlashCommand("exit", "/exit", {"ru": "выйти", "en": "quit"}, _exit, aliases=("quit",)),
)


def find_command(name: str) -> SlashCommand | None:
    for command in COMMANDS:
        if name == command.name or name in command.aliases:
            return command
    return None


def parse_command(text: str) -> tuple[str, str] | None:
    """``("mcp", "reconnect x")`` for ``"/mcp reconnect x"``; ``None`` if not a command."""

    stripped = text.strip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return None
    name, _, args = stripped[1:].partition(" ")
    return name, args.strip()


async def run_command(host: CommandHost, text: str) -> CommandResult:
    parsed = parse_command(text)
    if parsed is None:
        return CommandResult()
    name, args = parsed
    command = find_command(name)
    if command is None:
        hint = "неизвестная команда" if host.locale == "ru" else "unknown command"
        return CommandResult(messages=(Notice(f"{hint}: /{name} · /help", "warn"),))
    return await command.run(host, args)


class SlashCompleter(Completer):
    """Completes command names after ``/`` and their arguments after a space."""

    def __init__(self, host: CommandHost) -> None:
        self._host = host

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        name, space, rest = text[1:].partition(" ")
        if not space:
            for command in COMMANDS:
                if command.name.startswith(name):
                    yield Completion(
                        f"/{command.name}",
                        start_position=-len(text),
                        display=command.usage,
                        display_meta=command.describe(self._host.locale),
                    )
            return
        command = find_command(name)
        if command is None or command.complete_args is None:
            return
        last = rest.split(" ")[-1]
        for value in command.complete_args(self._host, rest):
            yield Completion(value, start_position=-len(last))


__all__ = [
    "COMMANDS",
    "CommandHost",
    "CommandResult",
    "SlashCommand",
    "SlashCompleter",
    "find_command",
    "parse_command",
    "run_command",
]
