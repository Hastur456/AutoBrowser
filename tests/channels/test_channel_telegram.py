"""The Telegram channel over a fake Bot API (``httpx.MockTransport``): long polling, the
allowlist, commands, approval buttons, backoff, and a token that never leaks."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from src.channels.telegram import (
    CALLBACK_DATA_MAX_BYTES,
    MESSAGE_MAX_CHARS,
    BotApi,
    TelegramChannel,
    TelegramConflict,
    TelegramRateLimited,
    TelegramUnauthorized,
)
from src.config import TelegramSettings
from src.serve.commands import ChatCommand, ChatReply
from src.serve.notify import Notification
from tests.serve.serve_fakes import START, FakeClock

TOKEN = "123456:SECRET-token-value"
ALLOWED = 1001
STRANGER = 666
NOW = int(START.timestamp())


class FakeBot:
    """The Bot API: scripted ``getUpdates`` answers, ``ok`` for everything else, every request
    kept. After the script ``getUpdates`` answers 401, which ends the poll loop."""

    def __init__(self, *updates: Any, webhook: str = "") -> None:
        self.updates = list(updates)
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.webhook = webhook
        self.responses: dict[str, list[httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.requests.append((method, body))
        scripted = self.responses.get(method)
        if scripted:
            return scripted.pop(0)
        if method == "getUpdates":
            if not self.updates:
                return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
            item = self.updates.pop(0)
            if isinstance(item, httpx.Response):
                return item
            if isinstance(item, Exception):
                raise item
            return httpx.Response(200, json={"ok": True, "result": item})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "PriceBot"}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": self.webhook, "pending_update_count": 3}})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [body for name, body in self.requests if name == method]


class Port:
    """A CommandPort that records what the channel asked."""

    def __init__(self, *, answers: bool = True) -> None:
        self.log: list[tuple[str, Any]] = []
        self.answers = answers

    async def handle(self, command: ChatCommand, *, channel: str, sender: str) -> ChatReply:
        self.log.append(("handle", (command, channel, sender)))
        return ChatReply(f"reply to {command.name}")

    def answer_approval(self, ticket_id: str, answer: str, *, channel: str, sender: str) -> bool:
        self.log.append(("answer", (ticket_id, answer, channel)))
        return self.answers

    def rejected(self, *, channel: str, reason: str, sender: str) -> None:
        self.log.append(("rejected", (reason, sender)))

    def channel_error(self, *, channel: str, error: str, retry_in: float) -> None:
        self.log.append(("error", (error, retry_in)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.log]


class Offsets:
    def __init__(self, start: int = 0, log: list[Any] | None = None) -> None:
        self.value = start
        self.log = log if log is not None else []

    def load(self, channel: str) -> int:
        return self.value

    def save(self, channel: str, offset: int) -> None:
        self.value = offset
        self.log.append(("save", offset))


def settings(**fields: Any) -> TelegramSettings:
    return TelegramSettings(bot_token=TOKEN, allowed_chat_ids=[ALLOWED], **fields)


def channel(bot: FakeBot, *, offsets: Offsets | None = None, clock: FakeClock | None = None,
            **fields: Any) -> tuple[TelegramChannel, FakeClock]:
    clock = clock or FakeClock()
    config = settings(**fields)
    api = BotApi(config, client=httpx.AsyncClient(transport=httpx.MockTransport(bot.handler)))
    return TelegramChannel(config, api=api, offsets=offsets or Offsets(), clock=clock), clock


def message(update_id: int, text: str, *, chat: int = ALLOWED, date: int = NOW) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat}, "date": date, "text": text}}


def press(update_id: int, data: str, *, chat: int = ALLOWED) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb-{update_id}", "data": data, "message": {"message_id": 5, "chat": {"id": chat}}},
    }


async def poll(chan: TelegramChannel, port: Port) -> None:
    with pytest.raises(TelegramUnauthorized):
        await chan.run(port)


# -- polling --------------------------------------------------------------------------------


async def test_a_command_from_an_allowed_chat_is_handled_and_answered() -> None:
    bot = FakeBot([message(10, "/status")])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    (command, name, sender), = [args for kind, args in port.log if kind == "handle"]
    assert command == ChatCommand("status") and name == "telegram"
    assert sender and str(ALLOWED) not in sender
    reply, = bot.calls("sendMessage")
    assert reply["chat_id"] == ALLOWED and reply["text"] == "reply to status"
    assert "parse_mode" not in reply
    assert reply["link_preview_options"] == {"is_disabled": True}
    first = bot.calls("getUpdates")[0]
    assert first["offset"] == 0 and first["timeout"] == 30
    assert first["allowed_updates"] == ["message", "callback_query"]


async def test_the_offset_is_saved_before_the_updates_are_handled() -> None:
    log: list[Any] = []
    bot = FakeBot([message(10, "/status"), message(11, "/jobs")])
    chan, _ = channel(bot, offsets=Offsets(log=log))
    port = Port()
    original = port.handle

    async def handle(command: ChatCommand, **kwargs: Any) -> ChatReply:
        log.append(("handle", command.name))
        return await original(command, **kwargs)

    port.handle = handle  # type: ignore[method-assign]

    await poll(chan, port)

    assert log == [("save", 11), ("handle", "status"), ("save", 12), ("handle", "jobs")]


async def test_a_restart_continues_from_the_saved_offset() -> None:
    bot = FakeBot([])
    chan, _ = channel(bot, offsets=Offsets(start=42))

    await poll(chan, Port())

    assert bot.calls("getUpdates")[0]["offset"] == 42


async def test_a_message_older_than_the_max_age_is_ignored() -> None:
    bot = FakeBot([message(10, "/run price-watch", date=NOW - 301)])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert port.log == [] and bot.calls("sendMessage") == []


async def test_a_stranger_is_rejected_and_never_answered() -> None:
    bot = FakeBot([message(10, "/run price-watch", chat=STRANGER)])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    (reason, sender), = [args for kind, args in port.log if kind == "rejected"]
    assert reason == "chat" and str(STRANGER) not in sender
    assert "handle" not in port.kinds()
    assert bot.calls("sendMessage") == []


async def test_the_bot_suffix_of_a_command_is_dropped() -> None:
    bot = FakeBot([message(10, "/run@PriceBot price-watch")])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    (command, _, _), = [args for kind, args in port.log if kind == "handle"]
    assert command == ChatCommand("run", "price-watch")


async def test_plain_text_gets_a_hint_and_no_command() -> None:
    bot = FakeBot([message(10, "ok, go")])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert "handle" not in port.kinds()
    assert "/help" in bot.calls("sendMessage")[0]["text"]


# -- approvals ------------------------------------------------------------------------------


TICKET = "approval-" + "a" * 32


async def test_an_approval_card_has_one_button_per_allowed_answer() -> None:
    bot = FakeBot()
    chan, _ = channel(bot)

    await chan.send(
        Notification(kind="approval", job_id="j", run_id="r", title="Approval needed", body="buy",
                     ticket_id=TICKET, answers=("once", "deny"))
    )

    sent, = bot.calls("sendMessage")
    buttons = [button for row in sent["reply_markup"]["inline_keyboard"] for button in row]
    assert [button["callback_data"] for button in buttons] == [f"a:{TICKET}:o", f"a:{TICKET}:d"]
    assert all(len(button["callback_data"].encode()) <= CALLBACK_DATA_MAX_BYTES for button in buttons)


async def test_a_ticket_too_long_for_the_buttons_is_sent_without_them() -> None:
    bot = FakeBot()
    chan, _ = channel(bot)

    await chan.send(
        Notification(kind="approval", job_id="j", run_id="r", title="t", ticket_id="x" * 70,
                     answers=("once", "deny"))
    )

    sent, = bot.calls("sendMessage")
    assert "reply_markup" not in sent and "API" in sent["text"]


async def test_a_button_press_answers_the_ticket_and_clears_the_keyboard() -> None:
    bot = FakeBot([press(10, f"a:{TICKET}:o")])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert [args for kind, args in port.log if kind == "answer"] == [(TICKET, "once", "telegram")]
    assert bot.calls("answerCallbackQuery")[0] == {"callback_query_id": "cb-10", "text": "answered"}
    cleared, = bot.calls("editMessageReplyMarkup")
    assert cleared["chat_id"] == ALLOWED and cleared["message_id"] == 5


async def test_an_unanswered_press_says_so_and_keeps_the_keyboard() -> None:
    bot = FakeBot([press(10, f"a:{TICKET}:d")])
    chan, _ = channel(bot)

    await poll(chan, Port(answers=False))

    assert "expired" in bot.calls("answerCallbackQuery")[0]["text"]
    # Expired, rate-limited or refused: a ticket that may still wait keeps its buttons.
    assert bot.calls("editMessageReplyMarkup") == []


async def test_without_chat_approvals_a_card_has_no_buttons() -> None:
    bot = FakeBot()
    chan, _ = channel(bot, approvals=False)

    await chan.send(
        Notification(kind="approval", job_id="j", run_id="r", title="Approval needed",
                     ticket_id=TICKET, answers=("once", "deny"))
    )

    assert "reply_markup" not in bot.calls("sendMessage")[0]


@pytest.mark.parametrize("data", ["x:y", f"a:{TICKET}:z", "a::o", ""])
async def test_a_malformed_button_answers_nothing(data: str) -> None:
    bot = FakeBot([press(10, data)])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert "answer" not in port.kinds()


async def test_a_strangers_button_press_is_rejected() -> None:
    bot = FakeBot([press(10, f"a:{TICKET}:o", chat=STRANGER)])
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert [args[0] for kind, args in port.log if kind == "rejected"] == ["callback"]
    assert "answer" not in port.kinds()
    assert bot.calls("answerCallbackQuery") == []


# -- sending --------------------------------------------------------------------------------


async def test_a_notification_goes_to_every_allowed_chat_as_plain_text() -> None:
    bot = FakeBot()
    config = TelegramSettings(bot_token=TOKEN, allowed_chat_ids=[1, 2])
    api = BotApi(config, client=httpx.AsyncClient(transport=httpx.MockTransport(bot.handler)))
    chan = TelegramChannel(config, api=api, offsets=Offsets(), clock=FakeClock())

    await chan.send(Notification(kind="change", job_id="j", run_id="r", title="j: changed", body="x" * 5000))

    sent = bot.calls("sendMessage")
    assert [item["chat_id"] for item in sent] == [1, 2]
    assert all(len(item["text"]) <= MESSAGE_MAX_CHARS for item in sent)
    assert sent[0]["text"].startswith("j: changed\n")


async def test_a_rate_limited_send_is_retried_once_within_the_limit() -> None:
    bot = FakeBot()
    bot.responses["sendMessage"] = [
        httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}),
    ]
    chan, clock = channel(bot)

    await chan.send(Notification(kind="success", job_id="j", run_id="r", title="t"))

    assert len(bot.calls("sendMessage")) == 2
    assert clock.sleeps == [3]


async def test_a_rate_limit_longer_than_the_limit_fails_the_send() -> None:
    bot = FakeBot()
    bot.responses["sendMessage"] = [
        httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 99}}),
    ]
    chan, clock = channel(bot)

    with pytest.raises(TelegramRateLimited):
        await chan.send(Notification(kind="success", job_id="j", run_id="r", title="t"))
    assert clock.sleeps == []


# -- failures -------------------------------------------------------------------------------


async def test_failed_polls_back_off_doubling_up_to_the_max_and_reset_on_success() -> None:
    bot = FakeBot(
        httpx.Response(502, json={"ok": False, "error_code": 502}),
        httpx.ConnectError("down"),
        httpx.Response(500, text="no json"),
        [],
        httpx.Response(502, json={"ok": False, "error_code": 502}),
    )
    chan, clock = channel(bot, poll_backoff_seconds=1.0, poll_backoff_max_seconds=3.0)
    port = Port()

    await poll(chan, port)

    errors = [args for kind, args in port.log if kind == "error"]
    assert errors == [
        ("TelegramUnavailable", 1.0),
        ("TelegramUnavailable", 2.0),
        ("TelegramUnavailable", 3.0),
        ("TelegramUnavailable", 1.0),
    ]
    assert clock.sleeps == [1.0, 2.0, 3.0, 1.0]


async def test_a_rate_limited_poll_waits_the_given_time() -> None:
    bot = FakeBot(httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}))
    chan, clock = channel(bot)
    port = Port()

    await poll(chan, port)

    assert [args for kind, args in port.log if kind == "error"] == [("TelegramRateLimited", 7.0)]


async def test_a_conflict_is_a_channel_error_and_polling_goes_on() -> None:
    bot = FakeBot(httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"}))
    chan, _ = channel(bot)
    port = Port()

    await poll(chan, port)

    assert [args[0] for kind, args in port.log if kind == "error"] == ["TelegramConflict"]
    assert len(bot.calls("getUpdates")) == 2


async def test_check_refuses_a_bot_with_a_webhook() -> None:
    bot = FakeBot(webhook=f"https://hooks.example/{TOKEN}/secret-path")
    chan, _ = channel(bot)

    with pytest.raises(TelegramConflict) as caught:
        await chan.check()

    assert "secret-path" not in str(caught.value) and TOKEN not in str(caught.value)
    assert "webhook" in str(caught.value)


async def test_check_passes_without_a_webhook() -> None:
    chan, _ = channel(FakeBot())

    await chan.check()


async def test_the_api_base_replaces_the_public_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.scheme}://{request.url.host}:{request.url.port}")
        return httpx.Response(200, json={"ok": True, "result": {"url": ""}})

    config = settings(api_base="http://localhost:8081")
    api = BotApi(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    await api.get_webhook_info()

    assert seen == ["http://localhost:8081"]


async def test_a_failed_offset_save_is_a_channel_error_and_polling_goes_on() -> None:
    class Broken(Offsets):
        def save(self, channel: str, offset: int) -> None:
            raise OSError("disk full")

    bot = FakeBot([message(10, "/status")])
    chan, _ = channel(bot, offsets=Broken())
    port = Port()

    await poll(chan, port)

    assert ("error", ("OSError", 0.0)) in port.log
    assert "handle" in port.kinds()


async def test_a_command_for_another_bot_is_ignored() -> None:
    bot = FakeBot([message(10, "/run@OtherBot price-watch"), message(11, "/run@pricebot price-watch")])
    chan, _ = channel(bot)
    port = Port()
    await chan.check()  # learns the bot's own name

    await poll(chan, port)

    commands = [args[0] for kind, args in port.log if kind == "handle"]
    assert commands == [ChatCommand("run", "price-watch")]


# -- the token never leaks ------------------------------------------------------------------


async def test_the_token_is_in_no_error_log_or_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bot = FakeBot(httpx.ConnectError(f"cannot reach https://api.example/bot{TOKEN}/getUpdates"),
                  httpx.Response(500, text=f"boom {TOKEN}"))
    chan, _ = channel(bot)
    port = Port()

    with pytest.raises(TelegramUnauthorized) as caught:
        await chan.run(port)
    await chan.aclose()

    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert TOKEN not in caplog.text
    assert "HTTP Request" in caplog.text  # httpx logged the calls, with the token redacted
    assert TOKEN not in str(port.log)


async def test_the_token_is_redacted_in_the_httpcore_child_loggers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    chan, _ = channel(FakeBot())

    for name in ("httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpx"):
        logging.getLogger(name).debug("request to /bot%s/getUpdates", TOKEN)
    await chan.aclose()

    assert TOKEN not in caplog.text
    assert caplog.text.count("[REDACTED]") == 5


async def test_the_sender_is_a_keyed_hash_of_the_chat() -> None:
    first, _ = channel(FakeBot())
    other = TelegramChannel(
        TelegramSettings(bot_token="999:other", allowed_chat_ids=[ALLOWED]),
        api=BotApi(TelegramSettings(bot_token="999:other"), client=httpx.AsyncClient(transport=httpx.MockTransport(FakeBot().handler))),
        offsets=Offsets(),
        clock=FakeClock(),
    )

    assert first.sender(ALLOWED) == first.sender(ALLOWED)
    assert first.sender(ALLOWED) != first.sender(STRANGER)
    assert first.sender(ALLOWED) != other.sender(ALLOWED)
    assert str(ALLOWED) not in first.sender(ALLOWED)


pytestmark = pytest.mark.asyncio
