"""The stand's chat commands: one neutral port for every chat channel.

A channel adapter (``src/channels/``) reads messages from its service, keeps out every chat
not on its allowlist and hands the rest here: commands (``/jobs``, ``/run <job>``,
``/status``, ``/budget``, ``/cancel [run|job]``, ``/task <text>``), the answers of approval
buttons, its refusals and its errors. The lexicon is the stand's, not a vendor's.

What a sender may do:

- ``/run`` fires only a job that has a trigger of the channel's own type (``type:
  telegram`` for the Telegram channel): the job's explicit consent to chat runs;
- ``/task`` exists only with ``serve.free_tasks`` and then runs like ``POST /v1/tasks``
  (``serve.free_task_domains``, the stand's default budget, no carried context);
- the text of a message never reaches a job's task.

Every command is a ``serve.command`` event with an opaque ``sender`` (the channel's keyed
hash of the chat), never the raw chat id and never a free task's text. Commands and refusals
are rate-limited per sender (``serve.rate_limit_per_minute``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.agent_loop.events import EventEmitter
from src.config import ServeSettings
from src.contracts import ApprovalAnswer
from src.serve.approvals import ApprovalsPort
from src.serve.auth import RateLimiter
from src.serve.clock import Clock
from src.serve.dispatcher import Dispatcher, FireResult, TriggerRef
from src.serve.notify import cut
from src.ui.feed import sanitize_text

ChatCommandName = Literal["help", "jobs", "run", "status", "budget", "cancel", "task", "unknown"]
#: A command is ``/<name> [argument]``.
COMMAND_PREFIX = "/"
#: Chat clients send ``/start`` on first contact; it means help.
COMMAND_ALIASES: Mapping[str, ChatCommandName] = {"start": "help"}
_NAMES: frozenset[str] = frozenset({"help", "jobs", "run", "status", "budget", "cancel", "task"})
#: Trigger id of a free task sent from a chat (``TriggerRef(<channel>, "task")``).
FREE_TASK_TRIGGER = "task"
#: A run id as the dispatcher makes them; anything else a sender typed is not logged.
_RUN_ID = re.compile(r"run-[0-9a-f]{32}")
_SOURCE = "serve.commands"

CommandOutcome = Literal["ok", "refused", "rate_limited", "unknown"]


@dataclass(frozen=True)
class ChatCommand:
    name: ChatCommandName
    #: Job id or run id; the task text for ``task``.
    argument: str = ""


@dataclass(frozen=True)
class ChatReply:
    """What the channel sends back; ``""``: nothing (a rate-limited sender)."""

    text: str


def parse_command(text: str) -> ChatCommand | None:
    """``/run price-watch`` -> ``ChatCommand("run", "price-watch")``; not a command -> ``None``.

    An unknown ``/name`` is ``ChatCommand("unknown")``. The channel strips its own syntax
    (Telegram's ``/run@Bot``) before.
    """

    stripped = text.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    head, _, rest = stripped[len(COMMAND_PREFIX):].partition(" ")
    name = head.strip().lower()
    name = COMMAND_ALIASES.get(name, name)
    if not name:
        return None
    if name not in _NAMES:
        return ChatCommand("unknown")
    return ChatCommand(name, " ".join(rest.split()))  # type: ignore[arg-type]


class CommandPort(Protocol):
    """What a chat channel may ask of the stand."""

    async def handle(self, command: ChatCommand, *, channel: str, sender: str) -> ChatReply: ...

    def answer_approval(
        self, ticket_id: str, answer: ApprovalAnswer, *, channel: str, sender: str
    ) -> bool: ...

    def rejected(self, *, channel: str, reason: str, sender: str) -> None: ...

    def channel_error(self, *, channel: str, error: str, retry_in: float) -> None: ...


class OffsetStore(Protocol):
    """Where a channel stopped reading (``ChannelStateStore``)."""

    def load(self, channel: str) -> int: ...

    def save(self, channel: str, offset: int) -> None: ...


class ServeCommands:
    """:class:`CommandPort` over the dispatcher and the approval desk."""

    def __init__(
        self,
        settings: ServeSettings,
        dispatcher: Dispatcher,
        *,
        approvals: ApprovalsPort | None,
        events: EventEmitter,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._approvals = approvals
        self._events = events
        self._commands = RateLimiter(settings.rate_limit_per_minute, clock)
        self._refusals = RateLimiter(settings.rate_limit_per_minute, clock)

    async def handle(self, command: ChatCommand, *, channel: str, sender: str) -> ChatReply:
        if not self._commands.allow(f"{channel}:{sender}"):
            self._record(command, channel=channel, sender=sender, outcome="rate_limited")
            return ChatReply("")
        text, outcome = await self._answer(command, channel)
        self._record(command, channel=channel, sender=sender, outcome=outcome)
        return ChatReply(cut(sanitize_text(text), self._settings.notify_max_chars))

    def answer_approval(
        self, ticket_id: str, answer: ApprovalAnswer, *, channel: str, sender: str
    ) -> bool:
        if not self._commands.allow(f"{channel}:{sender}"):
            self._emit_command(channel, "approve", "", sender, "rate_limited")
            return False
        answered = self._approvals is not None and self._approvals.answer(
            ticket_id, answer, via="channel"
        )
        self._emit_command(channel, "approve", ticket_id, sender, "ok" if answered else "unknown")
        return answered

    def rejected(self, *, channel: str, reason: str, sender: str) -> None:
        if self._refusals.allow(f"{channel}:{sender}"):
            self._emit("serve.auth.rejected", {"channel": channel, "reason": reason, "sender": sender})

    def channel_error(self, *, channel: str, error: str, retry_in: float) -> None:
        self._emit("serve.channel.error", {"channel": channel, "error": error, "retry_in": retry_in})

    # ------------------------------------------------------------------ commands

    async def _answer(self, command: ChatCommand, channel: str) -> tuple[str, CommandOutcome]:
        name = command.name
        if name == "help":
            return self._help(), "ok"
        if name == "jobs":
            return self._jobs(channel), "ok"
        if name == "run":
            return await self._run(command.argument, channel)
        if name == "status":
            return self._status(), "ok"
        if name == "budget":
            return self._budget(), "ok"
        if name == "cancel":
            return await self._cancel(command.argument)
        if name == "task":
            return await self._task(command.argument, channel)
        return "Unknown command. Send /help for the list.", "unknown"

    def _help(self) -> str:
        lines = [
            "/jobs - the stand's jobs",
            "/run <job> - run a job that allows chat runs",
            "/status - running runs, queue, today",
            "/budget - today against the day caps",
            "/cancel [run|job] - cancel a queued or running run",
        ]
        if self._settings.free_tasks:
            lines.append("/task <text> - run a free task")
        return "\n".join(lines)

    def _jobs(self, channel: str) -> str:
        if not self._settings.jobs:
            return "No jobs are configured."
        state = self._dispatcher.status()["jobs"]
        lines = []
        for job_id, job in self._settings.jobs.items():
            chat = "yes" if _chat_trigger(job.triggers, channel) is not None else "no"
            last = state.get(job_id, {}).get("last_finished_at") or "never"
            enabled = "enabled" if job.enabled else "disabled"
            lines.append(f"{job_id}: {enabled}, chat runs {chat}, last run {last}")
        return "\n".join(lines)

    async def _run(self, job_id: str, channel: str) -> tuple[str, CommandOutcome]:
        job = self._settings.jobs.get(job_id)
        if job is None:
            return "Unknown job. Send /jobs for the list.", "refused"
        trigger = _chat_trigger(job.triggers, channel)
        if trigger is None:
            return f"{job_id} is not runnable from the chat (it has no {channel} trigger).", "refused"
        result = await self._dispatcher.fire(job_id, TriggerRef(channel, trigger))
        return _fired(job_id, result), "ok"

    def _status(self) -> str:
        status = self._dispatcher.status()
        running = status.get("running")
        workers = status.get("workers")
        if isinstance(workers, list) and workers:
            busy = [item for item in workers if item.get("running")]
            now = ", ".join(
                f"w{item['index']}: {item['running']['job_id'] or 'task'} {item['running']['run_id']}"
                for item in busy
            ) or "none"
        elif running:
            now = f"{running['job_id'] or 'task'} {running['run_id']}"
        else:
            now = "none"
        day = status["day"]
        return (
            f"running: {now}\nqueue {len(status['queue'])}\n"
            f"today: runs {day['runs']}, tokens {day['tokens']}, cost {day['cost']:.4f}"
        )

    def _budget(self) -> str:
        day = self._dispatcher.status()["day"]
        return (
            f"runs {day['runs']}/{_cap(day['max_runs'])}, "
            f"tokens {day['tokens']}/{_cap(day['max_tokens'])}, "
            f"cost {day['cost']:.4f}/{_cap(day['max_cost'])}"
        )

    async def _cancel(self, target: str) -> tuple[str, CommandOutcome]:
        result = await self._dispatcher.cancel(target)
        if not result.cancelled and not result.stopping:
            return "There is nothing to cancel.", "refused"
        parts = []
        if result.cancelled:
            parts.append(f"cancelled {', '.join(result.cancelled)}")
        if result.stopping:
            parts.append(f"stopping {', '.join(result.stopping)}")
        return "; ".join(parts), "ok"

    async def _task(self, text: str, channel: str) -> tuple[str, CommandOutcome]:
        if not self._settings.free_tasks:
            return "Free tasks are off on this stand (serve.free_tasks).", "refused"
        if not text:
            return "Send the task after the command: /task <text>.", "refused"
        result = await self._dispatcher.submit_task(text, TriggerRef(channel, FREE_TASK_TRIGGER))
        return _fired("task", result), "ok"

    # ------------------------------------------------------------------ events

    def _record(self, command: ChatCommand, *, channel: str, sender: str, outcome: str) -> None:
        extra: dict[str, Any] = {}
        argument = ""
        if command.name == "task":
            extra["chars"] = len(command.argument)
        elif command.argument in self._settings.jobs or _RUN_ID.fullmatch(command.argument):
            argument = command.argument
        self._emit_command(channel, command.name, argument, sender, outcome, **extra)

    def _emit_command(
        self, channel: str, command: str, argument: str, sender: str, outcome: str, **extra: Any
    ) -> None:
        self._emit(
            "serve.command",
            {
                "channel": channel,
                "command": command,
                "argument": argument,
                "sender": sender,
                "outcome": outcome,
                **extra,
            },
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, source=_SOURCE, payload=payload)  # type: ignore[arg-type]


def _chat_trigger(triggers: Any, channel: str) -> str | None:
    """The id of the job's trigger of the channel's type; ``None`` when it has none."""

    return next((trigger.id for trigger in triggers if trigger.type == channel), None)


def _fired(what: str, result: FireResult) -> str:
    if result.state == "skipped":
        return f"{what}: skipped ({result.reason}), {result.run_id}"
    return f"{what}: {result.state}, {result.run_id}"


def _cap(value: Any) -> str:
    return "-" if value is None else str(value)


__all__ = [
    "COMMAND_ALIASES",
    "COMMAND_PREFIX",
    "FREE_TASK_TRIGGER",
    "ChatCommand",
    "ChatCommandName",
    "ChatReply",
    "CommandPort",
    "OffsetStore",
    "ServeCommands",
    "parse_command",
]
