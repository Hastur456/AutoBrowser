"""The chat command port of the stand (``src/serve/commands.py``): any chat channel's
commands, approval answers and refusals go through it, never through a vendor module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.serve.commands import ChatCommand, ServeCommands, parse_command
from tests.serve.serve_fakes import Rig, job

CHANNEL = "telegram"
SENDER = "sender-hash"


#: The settings a chat trigger needs: the channel and its token and allowlist.
CHAT: dict[str, Any] = {
    "channels": ["log", "telegram"],
    "telegram": {"bot_token": "123:token", "allowed_chat_ids": [1]},
}


def chat_job(**fields: Any) -> dict[str, Any]:
    return job(triggers=[{"id": "chat", "type": "telegram"}], **fields)


class Desk:
    """An ApprovalsPort that records answers."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.answers: list[tuple[str, str, str]] = []

    def pending(self) -> list[dict[str, Any]]:
        return [{"ticket_id": ticket} for ticket in sorted(self.known)]

    def answer(self, ticket_id: str, answer: str, *, via: str = "api") -> bool:
        self.answers.append((ticket_id, answer, via))
        return ticket_id in self.known


def commands(rig: Rig, *, approvals: Any = None) -> ServeCommands:
    return ServeCommands(
        rig.settings, rig.dispatcher, approvals=approvals, events=rig.events, clock=rig.clock
    )


async def ask(port: ServeCommands, text: str) -> str:
    command = parse_command(text)
    assert command is not None
    return (await port.handle(command, channel=CHANNEL, sender=SENDER)).text


# -- parsing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/run price-watch", ChatCommand("run", "price-watch")),
        ("  /RUN   price-watch  ", ChatCommand("run", "price-watch")),
        ("/start", ChatCommand("help")),
        ("/help", ChatCommand("help")),
        ("/task buy milk please", ChatCommand("task", "buy milk please")),
        ("/cancel", ChatCommand("cancel")),
        ("/nope x", ChatCommand("unknown")),
    ],
)
def test_commands_are_parsed(text: str, expected: ChatCommand) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["hello", "", "run price-watch", "  "])
def test_plain_text_is_not_a_command(text: str) -> None:
    assert parse_command(text) is None


# -- commands -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_lists_the_commands_and_task_only_with_free_tasks(tmp_path: Path) -> None:
    plain = await ask(commands(Rig(tmp_path / "a")), "/help")
    free = await ask(commands(Rig(tmp_path / "b", free_tasks=True)), "/help")

    for name in ("/jobs", "/run", "/status", "/budget", "/cancel"):
        assert name in plain
    assert "/task" not in plain
    assert "/task" in free


@pytest.mark.asyncio
async def test_jobs_lists_names_and_whether_the_chat_may_run_them(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": chat_job(), "secret-job": job(task="do a private thing")}, **CHAT)

    text = await ask(commands(rig), "/jobs")

    assert "price-watch" in text and "secret-job" in text
    assert "private thing" not in text  # the task text never leaves the stand


@pytest.mark.asyncio
async def test_run_fires_a_job_with_a_chat_trigger(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": chat_job()}, **CHAT)

    text = await ask(commands(rig), "/run price-watch")

    assert "queued" in text
    queued, = rig.payloads("serve.run.queued")
    assert queued["trigger"] == {"type": CHANNEL, "id": "chat"}


@pytest.mark.asyncio
async def test_run_refuses_a_job_without_a_chat_trigger_and_an_unknown_job(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job()})
    port = commands(rig)

    assert "not runnable from the chat" in await ask(port, "/run price-watch")
    assert "Unknown job" in await ask(port, "/run nope")
    assert rig.payloads("serve.run.queued") == []
    outcomes = [event["outcome"] for event in rig.payloads("serve.command")]
    assert outcomes == ["refused", "refused"]
    # An unknown name is not written to the events: it is the sender's free text.
    assert all(event["argument"] in ("price-watch", "") for event in rig.payloads("serve.command"))


@pytest.mark.asyncio
async def test_status_and_budget_report_the_day(tmp_path: Path) -> None:
    rig = Rig(tmp_path, daily_max_runs=5)
    port = commands(rig)

    assert "queue 0" in await ask(port, "/status")
    assert "runs 0/5" in await ask(port, "/budget")


@pytest.mark.asyncio
async def test_task_needs_free_tasks(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    assert "Free tasks are off" in await ask(commands(rig), "/task buy milk")
    assert rig.payloads("serve.run.queued") == []


@pytest.mark.asyncio
async def test_a_free_task_from_the_chat_is_queued_and_logged_without_its_text(tmp_path: Path) -> None:
    rig = Rig(tmp_path, free_tasks=True, free_task_domains=["shop.example"])

    text = await ask(commands(rig), "/task buy milk")
    await rig.drain()

    assert "queued" in text
    (task, profile), = rig.worker.calls
    assert task == "buy milk"
    assert profile.allowed_domains == ("shop.example",)
    assert profile.carry_context is False
    event, = rig.payloads("serve.command")
    assert event["command"] == "task" and event["argument"] == "" and event["chars"] == len("buy milk")
    assert "buy milk" not in str(rig.payloads("serve.command"))


@pytest.mark.asyncio
async def test_cancel_drops_a_queued_run_of_a_job(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": chat_job()}, **CHAT)
    port = commands(rig)
    await ask(port, "/run price-watch")

    text = await ask(port, "/cancel price-watch")

    assert "cancelled" in text
    skipped, = rig.payloads("serve.run.skipped")
    assert skipped["reason"] == "cancelled"
    await rig.drain()
    assert rig.worker.calls == []


@pytest.mark.asyncio
async def test_cancel_stops_the_running_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": chat_job()}, **CHAT)
    rig.worker.gate = asyncio.Event()
    port = commands(rig)
    await ask(port, "/run price-watch")
    running = asyncio.create_task(rig.drain())
    await rig.worker.started.wait()

    text = await ask(port, "/cancel")

    assert "stopping" in text
    assert rig.worker.cancelled == 1
    rig.worker.gate.set()
    await running


@pytest.mark.asyncio
async def test_cancel_without_anything_running(tmp_path: Path) -> None:
    assert "nothing to cancel" in await ask(commands(Rig(tmp_path)), "/cancel")


@pytest.mark.asyncio
async def test_commands_are_rate_limited_per_sender(tmp_path: Path) -> None:
    rig = Rig(tmp_path, rate_limit_per_minute=2)
    port = commands(rig)

    replies = [await ask(port, "/status") for _ in range(3)]
    other = (await port.handle(ChatCommand("status"), channel=CHANNEL, sender="other")).text

    assert replies[2] == "" and replies[0] and other
    assert [event["outcome"] for event in rig.payloads("serve.command")][2] == "rate_limited"
    rig.clock.advance(61)
    assert await ask(port, "/status")


@pytest.mark.asyncio
async def test_replies_are_sanitized_and_cut(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notify_max_chars=100, jobs={f"job-{index}": job() for index in range(30)})

    text = await ask(commands(rig), "/jobs")

    assert len(text) <= 100


# -- approvals, refusals, channel errors ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_answer_goes_to_the_desk_as_a_channel_answer(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    desk = Desk({"approval-1"})
    port = commands(rig, approvals=desk)

    assert port.answer_approval("approval-1", "once", channel=CHANNEL, sender=SENDER) is True
    assert port.answer_approval("approval-x", "deny", channel=CHANNEL, sender=SENDER) is False
    assert desk.answers == [("approval-1", "once", "channel"), ("approval-x", "deny", "channel")]


@pytest.mark.asyncio
async def test_without_an_approval_desk_no_answer_is_taken(tmp_path: Path) -> None:
    assert commands(Rig(tmp_path)).answer_approval("a", "once", channel=CHANNEL, sender=SENDER) is False


@pytest.mark.asyncio
async def test_a_rejected_sender_is_an_auth_event_at_most_once_per_window(tmp_path: Path) -> None:
    rig = Rig(tmp_path, rate_limit_per_minute=2)
    port = commands(rig)

    for _ in range(5):
        port.rejected(channel=CHANNEL, reason="chat", sender="stranger")

    events = rig.payloads("serve.auth.rejected")
    assert events == [{"channel": CHANNEL, "reason": "chat", "sender": "stranger"}] * 2


@pytest.mark.asyncio
async def test_a_channel_error_is_an_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    commands(rig).channel_error(channel=CHANNEL, error="TelegramUnavailable", retry_in=2.0)

    assert rig.payloads("serve.channel.error") == [
        {"channel": CHANNEL, "error": "TelegramUnavailable", "retry_in": 2.0}
    ]

