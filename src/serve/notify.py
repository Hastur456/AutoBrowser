"""Notifications of the stand and the channels that deliver them.

Bodies are page data: every title and body is stripped of terminal control sequences (as the
feed does) and cut to ``serve.notify_max_chars``. A failing channel never fails a run: it
becomes ``serve.notify.failed`` (the error type only, never its message).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from src.agent_loop.events import EventEmitter
from src.config import NotifyKind, WebhookOutSettings
from src.serve.auth import SIGNATURE_HEADER, sign
from src.ui.feed import sanitize_text

_LOG = logging.getLogger(__name__)

#: Marks a body cut to ``serve.notify_max_chars``.
TRUNCATED = "…"


@dataclass(frozen=True)
class Notification:
    kind: NotifyKind
    job_id: str
    run_id: str
    title: str
    body: str = ""
    cost: float | None = None
    #: The approval ticket an ``approval`` notification asks about (``api_approvals``).
    ticket_id: str = ""


class NotificationChannel(Protocol):
    name: str

    async def send(self, item: Notification) -> None: ...


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - len(TRUNCATED))] + TRUNCATED


class Notifier:
    """Sends one notification through every channel."""

    def __init__(
        self, channels: Sequence[NotificationChannel], events: EventEmitter, *, max_chars: int
    ) -> None:
        self._channels = list(channels)
        self._events = events
        self._max_chars = max_chars

    @property
    def channels(self) -> tuple[NotificationChannel, ...]:
        return tuple(self._channels)

    async def notify(self, item: Notification) -> tuple[str, ...]:
        """The names of the channels that took ``item``."""

        item = dataclasses.replace(
            item,
            title=_cut(sanitize_text(item.title), self._max_chars),
            body=_cut(sanitize_text(item.body), self._max_chars),
        )
        delivered: list[str] = []
        for channel in self._channels:
            try:
                await channel.send(item)
            except Exception as exc:  # noqa: BLE001 - a channel never fails a run
                _LOG.warning("notification channel %s failed: %s", channel.name, type(exc).__name__)
                self._events.emit(
                    "serve.notify.failed",
                    source="serve.notify",
                    payload={
                        "channel": channel.name,
                        "kind": item.kind,
                        "job_id": item.job_id,
                        "run_id": item.run_id,
                        "error": type(exc).__name__,
                    },
                )
                continue
            delivered.append(channel.name)
        return tuple(delivered)


class LogChannel:
    """The ``log`` channel: a ``serve.notified`` event and one line on stdout."""

    name = "log"

    def __init__(self, events: EventEmitter, write: Callable[[str], None] = print) -> None:
        self._events = events
        self._write = write

    async def send(self, item: Notification) -> None:
        self._events.emit(
            "serve.notified",
            source="serve.notify",
            payload={
                "channel": self.name,
                "kind": item.kind,
                "job_id": item.job_id,
                "run_id": item.run_id,
                "title": item.title,
                "body": item.body,
                "cost": item.cost,
                "ticket_id": item.ticket_id,
            },
        )
        line = f"[{item.kind}] {item.title}"
        self._write(f"{line}: {item.body}" if item.body else line)



class WebhookOutChannel:
    """The ``webhook`` channel: each notification POSTed as JSON to ``serve.webhook_out.url``,
    signed with ``X-AutoBrowser-Signature`` when a secret is set. A bridge to any messenger;
    no retries (a failure is ``serve.notify.failed``)."""

    name = "webhook"

    def __init__(self, settings: WebhookOutSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient()

    async def send(self, item: Notification) -> None:
        body = json.dumps(
            {
                "kind": item.kind,
                "job_id": item.job_id,
                "run_id": item.run_id,
                "title": item.title,
                "body": item.body,
                "cost": item.cost,
                "ticket_id": item.ticket_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._settings.secret is not None and self._settings.secret.get_secret_value():
            headers[SIGNATURE_HEADER] = sign(body, self._settings.secret.get_secret_value())
        response = await self._client.post(
            self._settings.url, content=body, headers=headers, timeout=self._settings.timeout_seconds
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["TRUNCATED", "LogChannel", "Notification", "NotificationChannel", "Notifier", "WebhookOutChannel"]
