"""The stand's HTTP API (Starlette; ``docs/development/2026-11-02-serve-mode-design.md`` 7.3).

``GET /healthz`` is open; everything under ``/v1`` needs ``serve.token`` (a webhook needs its
trigger's secret instead). A refused request is a bodyless ``401`` and a
``serve.auth.rejected`` event without the secret (and without the client address unless
``serve.log_client_addresses``). Bodies are read at most up to ``serve.max_body_bytes``; a
webhook's body is only measured and signature-checked, never handed to the job.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.agent_loop.events import EventEmitter
from src.config import ServeSettings, TriggerSpec
from src.serve.auth import (
    SIGNATURE_HEADER,
    IdempotencyCache,
    RateLimiter,
    bearer_secret,
    secret_matches,
    signature_matches,
)
from src.serve.checks import trigger_secret
from src.serve.clock import Clock
from src.serve.dispatcher import Dispatcher, FireResult, TriggerRef
from src.serve.profiles import free_task_profile

IDEMPOTENCY_HEADER = "Idempotency-Key"
_SOURCE = "serve.app"
_ANSWERS = frozenset({"once", "session", "deny"})
#: Rate-limit keys: the API token, one webhook trigger, one failing client.
_TOKEN_KEY = "token"


class ApprovalsPort(Protocol):
    """Pending approvals and how to answer one (``serve.api_approvals``)."""

    def pending(self) -> list[dict[str, Any]]: ...

    def answer(self, ticket_id: str, answer: str) -> bool: ...


class _TooLarge(Exception):
    pass


class ServeApi:
    def __init__(
        self,
        settings: ServeSettings,
        dispatcher: Dispatcher,
        *,
        events: EventEmitter,
        clock: Clock,
        approvals: ApprovalsPort | None = None,
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._events = events
        self._limiter = RateLimiter(settings.rate_limit_per_minute, clock)
        self._idempotent: IdempotencyCache[FireResult] = IdempotencyCache(
            settings.idempotency_window_seconds, clock
        )
        self._approvals = approvals

    # ------------------------------------------------------------------ routes

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def status(self, request: Request) -> Response:
        refused = self._authorize(request)
        if refused is not None:
            return refused
        body = self._dispatcher.status()
        if self._settings.api_approvals and self._approvals is not None:
            body["approvals"] = self._approvals.pending()
        body["free_task_domains"] = list(self._settings.free_task_domains)
        return JSONResponse(body)

    async def run_job(self, request: Request) -> Response:
        refused = self._authorize(request) or await self._body_refusal(request)
        if refused is not None:
            return refused
        job_id = request.path_params["job_id"]
        if job_id not in self._settings.jobs:
            return Response(status_code=404)
        return await self._fire(request, job_id, TriggerRef("api", "manual"))

    async def hook(self, request: Request) -> Response:
        job_id = request.path_params["job_id"]
        trigger_id = request.path_params["trigger_id"]
        client_key = self._client_key(request)
        if self._limiter.exceeded(client_key):
            return Response(status_code=429)
        try:
            body = await self._read_body(request)
        except _TooLarge:
            return Response(status_code=413)
        trigger = self._webhook(job_id, trigger_id)
        if trigger is None or not self._hook_authentic(trigger, request, body):
            return self._reject(request, client_key, "invalid")
        if not self._limiter.allow(f"hook:{job_id}/{trigger_id}"):
            return Response(status_code=429)
        return await self._fire(request, job_id, TriggerRef("webhook", trigger_id))

    async def run_status(self, request: Request) -> Response:
        refused = self._authorize(request)
        if refused is not None:
            return refused
        status = self._dispatcher.run_status(request.path_params["run_id"])
        return JSONResponse(status) if status is not None else Response(status_code=404)

    async def answer(self, request: Request) -> Response:
        refused = self._authorize(request)
        if refused is not None:
            return refused
        if not self._settings.api_approvals or self._approvals is None:
            return Response(status_code=404)
        data = await self._json(request)
        if isinstance(data, Response):
            return data
        answer = data.get("answer") if isinstance(data, dict) else None
        if answer not in _ANSWERS:
            return JSONResponse({"error": "answer must be once, session or deny"}, status_code=400)
        if not self._approvals.answer(request.path_params["ticket_id"], str(answer)):
            return Response(status_code=404)
        return JSONResponse({"answered": True})

    async def free_task(self, request: Request) -> Response:
        refused = self._authorize(request)
        if refused is not None:
            return refused
        if not self._settings.free_tasks:
            return Response(status_code=404)
        data = await self._json(request)
        if isinstance(data, Response):
            return data
        task = data.get("task") if isinstance(data, dict) else None
        if not isinstance(task, str) or not task.strip():
            return JSONResponse({"error": "task must be a non-empty string"}, status_code=400)
        run_id = self._dispatcher.new_run_id()
        trigger = TriggerRef("api", "task")
        profile = free_task_profile(
            self._settings,
            day_left=self._dispatcher.day_left(),
            labels={"mode": "serve", "job_id": "", "run_id": run_id, "trigger": trigger.label()},
        )
        result = await self._dispatcher.submit_task(task.strip(), profile, trigger)
        return _fired(result)

    # ------------------------------------------------------------------ helpers

    def _authorize(self, request: Request) -> Response | None:
        client_key = self._client_key(request)
        if self._limiter.exceeded(client_key):
            return Response(status_code=429)
        token = self._settings.token
        if token is None or not token.get_secret_value().strip():
            return self._reject(request, client_key, "no_token")
        given = bearer_secret(request.headers.get("authorization"))
        if given is None:
            return self._reject(request, client_key, "missing")
        if not secret_matches(given, token):
            return self._reject(request, client_key, "invalid")
        if not self._limiter.allow(_TOKEN_KEY):
            return Response(status_code=429)
        return None

    def _reject(self, request: Request, client_key: str, reason: str) -> Response:
        self._limiter.allow(client_key)
        payload: dict[str, Any] = {"path": request.url.path, "reason": reason}
        if self._settings.log_client_addresses and request.client is not None:
            payload["client"] = request.client.host
        self._events.emit("serve.auth.rejected", source=_SOURCE, payload=payload)
        return Response(status_code=401)

    @staticmethod
    def _client_key(request: Request) -> str:
        host = request.client.host if request.client is not None else ""
        return f"client:{host}"

    def _webhook(self, job_id: str, trigger_id: str) -> TriggerSpec | None:
        job = self._settings.jobs.get(job_id)
        if job is None:
            return None
        return next(
            (t for t in job.triggers if t.id == trigger_id and t.type == "webhook"), None
        )

    @staticmethod
    def _hook_authentic(trigger: TriggerSpec, request: Request, body: bytes) -> bool:
        secret = trigger_secret(trigger)
        if trigger.auth == "hmac_sha256":
            return signature_matches(body, request.headers.get(SIGNATURE_HEADER), secret)
        return secret_matches(bearer_secret(request.headers.get("authorization")), secret)

    async def _fire(self, request: Request, job_id: str, trigger: TriggerRef) -> Response:
        key = request.headers.get(IDEMPOTENCY_HEADER)
        path = request.url.path
        if key:
            known = self._idempotent.get(path, key)
            if known is not None:
                return _fired(known)
        result = await self._dispatcher.fire(job_id, trigger)
        if key:
            self._idempotent.put(path, key, result)
        return _fired(result)

    async def _body_refusal(self, request: Request) -> Response | None:
        try:
            await self._read_body(request)
        except _TooLarge:
            return Response(status_code=413)
        return None

    async def _read_body(self, request: Request) -> bytes:
        limit = self._settings.max_body_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise _TooLarge
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise _TooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    async def _json(self, request: Request) -> Any:
        try:
            body = await self._read_body(request)
        except _TooLarge:
            return Response(status_code=413)
        try:
            return json.loads(body or b"null")
        except ValueError:
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)


def _fired(result: FireResult) -> Response:
    body = {"run_id": result.run_id, "state": result.state}
    if result.reason:
        body["reason"] = result.reason
    return JSONResponse(body, status_code=202)


def build_app(
    settings: ServeSettings,
    dispatcher: Dispatcher,
    *,
    events: EventEmitter,
    clock: Clock,
    approvals: ApprovalsPort | None = None,
) -> Starlette:
    api = ServeApi(settings, dispatcher, events=events, clock=clock, approvals=approvals)
    return Starlette(
        routes=[
            Route("/healthz", api.health, methods=["GET"]),
            Route("/v1/status", api.status, methods=["GET"]),
            Route("/v1/jobs/{job_id}/runs", api.run_job, methods=["POST"]),
            Route("/v1/hooks/{job_id}/{trigger_id}", api.hook, methods=["POST"]),
            Route("/v1/runs/{run_id}", api.run_status, methods=["GET"]),
            Route("/v1/approvals/{ticket_id}", api.answer, methods=["POST"]),
            Route("/v1/tasks", api.free_task, methods=["POST"]),
        ]
    )


__all__ = ["IDEMPOTENCY_HEADER", "ApprovalsPort", "ServeApi", "build_app"]
