"""The Telegram channel: notifications, chat commands and approval buttons over the Bot API.

Long polling (``getUpdates``), so the stand opens no inbound port; the Bot API refuses
polling while the bot has a webhook, which :meth:`TelegramChannel.check` reports before the
stand starts. Only the chats in ``serve.telegram.allowed_chat_ids`` are answered and notified;
any other chat is a ``serve.auth.rejected`` with an opaque sender and gets no answer.

The bot token is part of every request path (``/bot<token>/<method>``), so:

- every failed call becomes a :class:`TelegramError` naming the method and the status code
  only, raised ``from None`` (no URL, no ``httpx`` text, no chained cause);
- while a :class:`BotApi` is open, the ``httpx``/``httpcore`` loggers replace the token by
  :data:`REDACTED`;
- this module logs exception class names only.

Messages are plain text (no ``parse_mode``: page text cannot inject markup) without link
previews (Telegram does not fetch links taken from pages). The only module that names the
vendor; ``src/cli/`` is its only importer.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import TelegramSettings
from src.contracts import ApprovalAnswer
from src.serve.clock import Clock
from src.serve.commands import CommandPort, OffsetStore, parse_command
from src.serve.notify import Notification

_LOG = logging.getLogger(__name__)

#: Name of the channel: in ``serve.channels``, events, and the trigger type ``telegram``.
CHANNEL_NAME = "telegram"
#: The public Bot API; ``serve.telegram.api_base`` (a self-hosted Bot API server) replaces it.
TELEGRAM_API_BASE = "https://api.telegram.org"
#: Bot API limits: facts of the protocol, not tunables.
CALLBACK_DATA_MAX_BYTES = 64
MESSAGE_MAX_CHARS = 4096
#: ``callback_data`` of an approval button: ``a:<ticket_id>:<o|s|d>``.
CALLBACK_PREFIX = "a"
CALLBACK_SEPARATOR = ":"
ANSWER_CODES: Mapping[str, ApprovalAnswer] = {"o": "once", "s": "session", "d": "deny"}
_CODE_OF: Mapping[str, str] = {answer: code for code, answer in ANSWER_CODES.items()}
ANSWER_LABELS: Mapping[str, str] = {"once": "Allow once", "session": "Allow for the session", "deny": "Deny"}
#: Update types the bot asks for; nothing else is delivered.
ALLOWED_UPDATES = ("message", "callback_query")
#: What replaces the token in a log line.
REDACTED = "[REDACTED]"
#: Loggers that print request URLs (and so the token).
_HTTP_LOGGERS = ("httpx", "httpcore")
#: ``/run@PriceBot job`` -> ``/run job``: Telegram's command suffix in group chats.
_BOT_SUFFIX = re.compile(r"^(/\w+)@\w+")
#: Texts sent to the chats (the stand's lexicon, not page data).
HINT = "Send /help for the commands."
NO_BUTTONS = "\n(answer it via the API: the ticket id is too long for buttons)"
ANSWERED, EXPIRED = "answered", "expired"


class TelegramError(RuntimeError):
    """A failed Bot API call: the method and the status code, never the URL or a body."""

    def __init__(self, method: str, status_code: int | None = None, *, retry_after: float | None = None,
                 detail: str = "") -> None:
        self.method = method
        self.status_code = status_code
        self.retry_after = retry_after
        status = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"Telegram {method} failed{status}{': ' + detail if detail else ''}")


class TelegramUnauthorized(TelegramError):
    """401/404: the token is wrong or revoked; the channel cannot work."""


class TelegramConflict(TelegramError):
    """409: a webhook is set, or another process polls the same bot."""


class TelegramRateLimited(TelegramError):
    """429 with ``parameters.retry_after``."""


class TelegramUnavailable(TelegramError):
    """No answer: a connection error, a timeout or a 5xx."""


class TelegramBadRequest(TelegramError):
    """Any other refusal (a chat not found, a message too old to edit)."""


@dataclass(frozen=True)
class WebhookInfo:
    #: A webhook is set (its URL is never kept: it may hold a secret path).
    set: bool
    pending_updates: int


class _TokenFilter(logging.Filter):
    """Replaces the token in every record of a logger it is attached to."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._token:
            return True
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record still must not leak the token
            text = str(record.msg)
        if self._token in text:
            record.msg = text.replace(self._token, REDACTED)
            record.args = ()
        return True


class BotApi:
    """The Bot API calls the channel needs, over one ``httpx.AsyncClient``."""

    def __init__(self, settings: TelegramSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._token = settings.bot_token.get_secret_value().strip() if settings.bot_token else ""
        self._base = (settings.api_base or TELEGRAM_API_BASE).rstrip("/")
        self._client = client or httpx.AsyncClient()
        self._filter = _TokenFilter(self._token)
        for name in _HTTP_LOGGERS:
            logging.getLogger(name).addFilter(self._filter)

    @property
    def token(self) -> str:
        return self._token

    async def get_updates(self, offset: int) -> list[Mapping[str, Any]]:
        timeout = self._settings.poll_timeout_seconds
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": list(ALLOWED_UPDATES)},
            timeout=timeout + self._settings.request_timeout_seconds,
        )
        return [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []

    async def send_message(
        self, chat_id: int, text: str, *, keyboard: Sequence[Sequence[Mapping[str, str]]] = ()
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MESSAGE_MAX_CHARS],
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": [list(row) for row in keyboard]}
        result = await self._call("sendMessage", payload)
        return int(result.get("message_id") or 0) if isinstance(result, Mapping) else 0

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    async def get_webhook_info(self) -> WebhookInfo:
        result = await self._call("getWebhookInfo", {})
        data = result if isinstance(result, Mapping) else {}
        return WebhookInfo(set=bool(data.get("url")), pending_updates=int(data.get("pending_update_count") or 0))

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            for name in _HTTP_LOGGERS:
                logging.getLogger(name).removeFilter(self._filter)

    async def _call(self, method: str, payload: Mapping[str, Any], *, timeout: float | None = None) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            response = await self._client.post(
                url, json=dict(payload), timeout=timeout or self._settings.request_timeout_seconds
            )
        except httpx.HTTPError:
            raise TelegramUnavailable(method) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, Mapping):
            data = {}
        status = response.status_code
        if status == 200 and data.get("ok") is True:
            return data.get("result")
        code = int(data.get("error_code") or status)
        if code == 429:
            parameters = data.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
            raise TelegramRateLimited(
                method, code, retry_after=float(retry_after) if retry_after is not None else None
            ) from None
        if code in (401, 404):
            raise TelegramUnauthorized(method, code) from None
        if code == 409:
            raise TelegramConflict(method, code) from None
        if code >= 500 or status >= 500:
            raise TelegramUnavailable(method, code) from None
        raise TelegramBadRequest(method, code) from None


class TelegramChannel:
    """``NotificationChannel`` ``telegram`` and its long-polling service (:meth:`run`)."""

    name = CHANNEL_NAME

    def __init__(self, settings: TelegramSettings, *, api: BotApi, offsets: OffsetStore, clock: Clock) -> None:
        self._settings = settings
        self._api = api
        self._offsets = offsets
        self._clock = clock
        self._allowed = frozenset(settings.allowed_chat_ids)

    def sender(self, chat_id: Any) -> str:
        """The opaque sender key of a chat: an HMAC of its id under the bot token."""

        return hmac.new(self._api.token.encode("utf-8"), str(chat_id).encode("utf-8"), hashlib.sha256).hexdigest()

    async def check(self) -> None:
        """Before the stand starts: the token works and no webhook blocks polling."""

        info = await self._api.get_webhook_info()
        if info.set:
            raise TelegramConflict(
                "getWebhookInfo",
                detail="the bot has a webhook; long polling needs it removed (deleteWebhook)",
            )

    async def send(self, item: Notification) -> None:
        """Send ``item`` to every allowed chat; an approval card gets its answer buttons."""

        text = item.title if not item.body else f"{item.title}\n{item.body}"
        keyboard: list[list[dict[str, str]]] = []
        if item.kind == "approval" and item.ticket_id and item.answers:
            buttons = [
                {"text": ANSWER_LABELS.get(answer, answer), "callback_data": _callback_data(item.ticket_id, answer)}
                for answer in item.answers
            ]
            if all(len(button["callback_data"].encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES for button in buttons):
                keyboard = [buttons]
            else:
                text += NO_BUTTONS
        failure: TelegramError | None = None
        for chat_id in self._settings.allowed_chat_ids:
            try:
                await self._send_once_more_if_limited(chat_id, text[:MESSAGE_MAX_CHARS], keyboard)
            except TelegramError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def run(self, commands: CommandPort) -> None:
        """Poll until cancelled; a bad token (:class:`TelegramUnauthorized`) ends the service."""

        offset = self._offsets.load(self.name)
        backoff = self._settings.poll_backoff_seconds
        while True:
            try:
                updates = await self._api.get_updates(offset)
            except TelegramUnauthorized:
                raise
            except TelegramError as exc:
                delay = exc.retry_after if isinstance(exc, TelegramRateLimited) and exc.retry_after else backoff
                commands.channel_error(channel=self.name, error=type(exc).__name__, retry_in=delay)
                await self._clock.sleep(delay)
                backoff = min(backoff * 2, self._settings.poll_backoff_max_seconds)
                continue
            backoff = self._settings.poll_backoff_seconds
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    # At most once: saved before handling, so a crash never repeats a command.
                    offset = max(offset, update_id + 1)
                    self._offsets.save(self.name, offset)
                try:
                    await self._handle(update, commands)
                except TelegramUnauthorized:
                    raise
                except Exception as exc:  # noqa: BLE001 - one update never stops the channel
                    _LOG.warning("telegram update not handled: %s", type(exc).__name__)

    async def aclose(self) -> None:
        await self._api.aclose()

    # ------------------------------------------------------------------ updates

    async def _handle(self, update: Mapping[str, Any], commands: CommandPort) -> None:
        if isinstance(update.get("message"), Mapping):
            await self._message(update["message"], commands)
        elif isinstance(update.get("callback_query"), Mapping):
            await self._callback(update["callback_query"], commands)

    async def _message(self, message: Mapping[str, Any], commands: CommandPort) -> None:
        chat_id = _chat_id(message)
        if chat_id not in self._allowed:
            commands.rejected(channel=self.name, reason="chat", sender=self.sender(chat_id))
            return
        date = message.get("date")
        if isinstance(date, (int, float)) and self._clock.now().timestamp() - date > self._settings.max_update_age_seconds:
            return
        text = str(message.get("text") or "")
        command = parse_command(_BOT_SUFFIX.sub(r"\1", text.strip()))
        if command is None:
            await self._api.send_message(chat_id, HINT)
            return
        reply = await commands.handle(command, channel=self.name, sender=self.sender(chat_id))
        if reply.text:
            await self._api.send_message(chat_id, reply.text)

    async def _callback(self, query: Mapping[str, Any], commands: CommandPort) -> None:
        message = query.get("message")
        chat_id = _chat_id(message) if isinstance(message, Mapping) else None
        if chat_id is None or chat_id not in self._allowed:
            commands.rejected(channel=self.name, reason="callback", sender=self.sender(chat_id))
            return
        parsed = _parse_callback(str(query.get("data") or ""))
        if parsed is None:
            return
        ticket_id, answer = parsed
        answered = commands.answer_approval(ticket_id, answer, channel=self.name, sender=self.sender(chat_id))
        await self._api.answer_callback(str(query.get("id") or ""), ANSWERED if answered else EXPIRED)
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if isinstance(message_id, int):
            await self._api.clear_keyboard(chat_id, message_id)

    async def _send_once_more_if_limited(
        self, chat_id: int, text: str, keyboard: Sequence[Sequence[Mapping[str, str]]]
    ) -> None:
        try:
            await self._api.send_message(chat_id, text, keyboard=keyboard)
        except TelegramRateLimited as exc:
            wait = exc.retry_after
            if wait is None or wait <= 0 or wait > self._settings.send_retry_max_seconds:
                raise
            await self._clock.sleep(wait)
            await self._api.send_message(chat_id, text, keyboard=keyboard)


def _chat_id(message: Any) -> Any:
    chat = message.get("chat") if isinstance(message, Mapping) else None
    return chat.get("id") if isinstance(chat, Mapping) else None


def _callback_data(ticket_id: str, answer: str) -> str:
    return CALLBACK_SEPARATOR.join((CALLBACK_PREFIX, ticket_id, _CODE_OF.get(answer, "")))


def _parse_callback(data: str) -> tuple[str, ApprovalAnswer] | None:
    parts = data.split(CALLBACK_SEPARATOR)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or not parts[1] or parts[2] not in ANSWER_CODES:
        return None
    return parts[1], ANSWER_CODES[parts[2]]


__all__ = [
    "ALLOWED_UPDATES",
    "CALLBACK_DATA_MAX_BYTES",
    "CHANNEL_NAME",
    "MESSAGE_MAX_CHARS",
    "TELEGRAM_API_BASE",
    "BotApi",
    "TelegramBadRequest",
    "TelegramChannel",
    "TelegramConflict",
    "TelegramError",
    "TelegramRateLimited",
    "TelegramUnauthorized",
    "TelegramUnavailable",
    "WebhookInfo",
]
