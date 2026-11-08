"""The outgoing webhook channel: signed JSON, a failure is a logged event, never the run's."""

from __future__ import annotations

import json

import httpx
import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.config import WebhookOutSettings
from src.serve.auth import signature_matches
from src.serve.notify import Notification, Notifier, WebhookOutChannel

ITEM = Notification(kind="change", job_id="price-watch", run_id="run-1", title="changed", body="Price 12", cost=0.01)


def channel(handler, **fields) -> tuple[WebhookOutChannel, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = WebhookOutSettings(url="https://hooks.example/autobrowser", **fields)
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return WebhookOutChannel(settings, client=client), seen


@pytest.mark.asyncio
async def test_a_notification_is_posted_as_signed_json() -> None:
    out, seen = channel(lambda _r: httpx.Response(204), secret="k3y")

    await out.send(ITEM)

    request, = seen
    body = json.loads(request.content)
    assert str(request.url) == "https://hooks.example/autobrowser"
    assert request.headers["content-type"] == "application/json"
    assert body == {"kind": "change", "job_id": "price-watch", "run_id": "run-1", "title": "changed",
                    "body": "Price 12", "cost": 0.01, "ticket_id": ""}
    assert signature_matches(request.content, request.headers["x-autobrowser-signature"], "k3y")


@pytest.mark.asyncio
async def test_without_a_secret_the_post_is_unsigned() -> None:
    out, seen = channel(lambda _r: httpx.Response(200))

    await out.send(ITEM)

    assert "x-autobrowser-signature" not in seen[0].headers


@pytest.mark.parametrize(
    "handler",
    [lambda _r: httpx.Response(503), lambda _r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow"))],
)
@pytest.mark.asyncio
async def test_a_failing_endpoint_is_a_notify_failed_event_without_the_secret(handler) -> None:
    out, _ = channel(handler, secret="k3y")
    sink = InMemoryEventSink()

    delivered = await Notifier([out], EventEmitter(sink), max_chars=1500).notify(ITEM)

    assert delivered == ()
    failed, = [r for r in sink.records if r.type == "serve.notify.failed"]
    assert failed.payload["channel"] == "webhook"
    assert "k3y" not in str(sink.records)


@pytest.mark.asyncio
async def test_the_timeout_comes_from_the_settings() -> None:
    out, seen = channel(lambda _r: httpx.Response(200), timeout_seconds=3.5)

    await out.send(ITEM)

    assert seen[0].extensions["timeout"]["read"] == 3.5
