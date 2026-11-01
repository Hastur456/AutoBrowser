"""The narrow Langfuse REST + OTLP/JSON client: the only module with Langfuse endpoints.

Built on ``httpx`` (already a dependency): Basic auth from ``eval_platform`` keys, the
``x-langfuse-ingestion-version: 4`` header on trace export, client-side rate limiting of the
REST calls, retries with backoff on 429/5xx/network errors (``Retry-After`` first), and no
retry where Langfuse forbids it: a partially accepted score batch (207), or a trace export
whose request may already have arrived (Langfuse v4 does not deduplicate a re-sent span).
Errors carry the method, path and status only — never headers, keys or request bodies.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from src.config import EvalPlatformSettings

logger = logging.getLogger(__name__)

API = "/api/public"
#: Direct OTLP exporters must send it, or ingestion lags by minutes (OpenAPI, 2026-10-31).
INGESTION_VERSION_HEADER = {"x-langfuse-ingestion-version": "4"}
#: Oldest server with the v4 data model this bridge writes (experiments over OTLP).
MIN_SERVER_VERSION = (4, 59, 0)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_RATE_WINDOW_SECONDS = 60.0  # requests_per_minute is per minute
#: Transport errors raised before the request reached the server: always safe to retry.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_AUTH_HINT = "check eval_platform.public_key/secret_key and the host's region"


class LangfuseError(Exception):
    """A failed Langfuse call: method, path, status and a reason, never headers or bodies."""

    def __init__(
        self,
        reason: str,
        *,
        method: str = "",
        path: str = "",
        status: int | None = None,
        retriable: bool = False,
        delivery_unknown: bool = False,
    ) -> None:
        self.reason = reason
        self.method = method
        self.path = path
        self.status = status
        self.retriable = retriable
        #: The request may have been received (a timeout after sending): never re-send blindly.
        self.delivery_unknown = delivery_unknown
        super().__init__(str(self))

    def __str__(self) -> str:
        where = f"{self.method} {self.path}: " if self.method else ""
        return f"{where}{self.reason}"

    def __repr__(self) -> str:
        return f"LangfuseError({str(self)!r}, status={self.status})"


@dataclass(frozen=True)
class HealthInfo:
    version: str
    status: str

    @property
    def compatible(self) -> bool:
        match = _VERSION.match(self.version.strip())
        if match is None:
            return False
        return tuple(int(part) for part in match.groups()) >= MIN_SERVER_VERSION


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    name: str


@dataclass(frozen=True)
class ScoreBatchResult:
    accepted: int
    rejected: int
    #: ``{"index", "id", "message"}`` of each rejected score, as the server reported them.
    errors: list[dict[str, Any]] = field(default_factory=list)


class LangfuseClient:
    def __init__(
        self,
        settings: EvalPlatformSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.host:
            raise LangfuseError("eval_platform.host is not set")
        public = settings.public_key.get_secret_value() if settings.public_key else ""
        secret = settings.secret_key.get_secret_value() if settings.secret_key else ""
        if not public.strip() or not secret.strip():
            raise LangfuseError("eval_platform.public_key and eval_platform.secret_key must both be set")
        self.host = settings.host
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._http = httpx.Client(
            base_url=settings.host,
            auth=httpx.BasicAuth(public, secret),
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LangfuseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- service --------------------------------------------------------------------------------

    def health(self) -> HealthInfo:
        body = self._json("GET", "/health")
        return HealthInfo(version=str(body.get("version") or ""), status=str(body.get("status") or ""))

    def project(self) -> ProjectInfo:
        projects = self._json("GET", "/projects").get("data") or []
        if not projects:
            raise LangfuseError("the keys belong to no project", method="GET", path=f"{API}/projects")
        return ProjectInfo(id=str(projects[0].get("id")), name=str(projects[0].get("name") or ""))

    # -- datasets -------------------------------------------------------------------------------

    def dataset(self, name: str) -> dict[str, Any] | None:
        """The dataset named ``name``, ``None`` when it does not exist."""

        try:
            return self._json("GET", f"/v2/datasets/{quote(name, safe='')}")
        except LangfuseError as exc:
            if exc.status == 404:
                return None
            raise

    def ensure_dataset(self, name: str, metadata: Mapping[str, Any]) -> str:
        """The dataset's id; created, or its metadata updated, only when needed."""

        dataset = self.dataset(name)
        if dataset is not None and (dataset.get("metadata") or {}) == dict(metadata):
            return str(dataset["id"])
        created = self._json("POST", "/v2/datasets", json={"name": name, "metadata": dict(metadata)})
        return str(created["id"])

    def dataset_items(self, dataset: str) -> list[dict[str, Any]]:
        return list(self._pages("/dataset-items", {"datasetName": dataset}))

    def upsert_dataset_item(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/dataset-items", json=dict(body))

    # -- traces ---------------------------------------------------------------------------------

    def export_otlp(self, payload: Mapping[str, Any]) -> None:
        """Send one OTLP/JSON request. A request that may have arrived is never re-sent: the
        error then has ``delivery_unknown`` and the caller verifies before trying again."""

        self._request(
            "POST",
            "/otel/v1/traces",
            json=dict(payload),
            headers=INGESTION_VERSION_HEADER,
            idempotent=False,
            throttled=False,
        )

    def trace_exists(self, trace_id: str) -> bool:
        body = self._json("GET", "/v2/observations", params={"traceId": trace_id, "limit": "1"})
        return bool(body.get("data"))

    def observation_ids(self, trace_id: str) -> list[str]:
        """The ids of every observation of a trace (every cursor page)."""

        params = {"traceId": trace_id}
        ids: list[str] = []
        while True:
            body = self._json("GET", "/v2/observations", params=params)
            ids += [str(item.get("id")) for item in body.get("data") or []]
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return ids
            params["cursor"] = str(cursor)

    # -- scores ---------------------------------------------------------------------------------

    def post_scores(self, scores: Sequence[Mapping[str, Any]]) -> ScoreBatchResult:
        """Upsert scores by ``id``. A 207 (partly rejected) is reported, never retried."""

        response = self._request("POST", "/scores", json=[dict(score) for score in scores])
        body = _body(response)
        if response.status_code == 207 or (isinstance(body, dict) and "rejected" in body):
            return ScoreBatchResult(
                accepted=int(body.get("accepted") or 0),
                rejected=int(body.get("rejected") or 0),
                errors=[dict(error) for error in body.get("errors") or []],
            )
        return ScoreBatchResult(accepted=len(scores), rejected=0)

    def scores(self, **filters: str) -> Iterator[dict[str, Any]]:
        """Scores of ``GET /v3/scores`` with the given filters, every cursor page."""

        params = dict(filters)
        while True:
            body = self._json("GET", "/v3/scores", params=params)
            yield from body.get("data") or []
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return
            params["cursor"] = str(cursor)

    def score_configs(self) -> list[dict[str, Any]]:
        return list(self._pages("/score-configs", {}))

    def create_score_config(self, body: Mapping[str, Any]) -> str:
        return str(self._json("POST", "/score-configs", json=dict(body))["id"])

    def archive_score_config(self, config_id: str) -> None:
        self._json("PATCH", f"/score-configs/{quote(config_id, safe='')}", json={"isArchived": True})

    # -- annotation queues ----------------------------------------------------------------------

    def ensure_queue(self, name: str, config_ids: Sequence[str]) -> str:
        for queue in self._pages("/annotation-queues", {}):
            if queue.get("name") == name:
                return str(queue["id"])
        body = {"name": name, "scoreConfigIds": list(config_ids)}
        return str(self._json("POST", "/annotation-queues", json=body)["id"])

    def enqueue(self, queue_id: str, trace_id: str) -> None:
        body = {"objectId": trace_id, "objectType": "TRACE"}
        self._json("POST", f"/annotation-queues/{quote(queue_id, safe='')}/items", json=body)

    # -- transport ------------------------------------------------------------------------------

    def _pages(self, path: str, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            body = self._json("GET", path, params={**params, "page": str(page)})
            yield from body.get("data") or []
            if page >= int((body.get("meta") or {}).get("totalPages") or 1):
                return
            page += 1

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return _body(self._request(method, path, **kwargs))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        throttled: bool = True,
    ) -> httpx.Response:
        full_path = f"{API}{path}"
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            if throttled:
                self._throttle()
            try:
                response = self._http.request(method, full_path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if not idempotent and not isinstance(exc, _NOT_SENT):
                    raise LangfuseError(
                        f"{type(exc).__name__}: the request may have arrived",
                        method=method,
                        path=full_path,
                        delivery_unknown=True,
                    ) from None
                if last:
                    raise LangfuseError(type(exc).__name__, method=method, path=full_path, retriable=True) from None
                self._backoff(attempt, None, method, full_path, type(exc).__name__)
                continue
            status = response.status_code
            if status < 400:
                return response
            if not idempotent and status >= 500 and status != 503:
                # A gateway error may come after the request was forwarded and ingested; a
                # re-send would duplicate spans, so the caller verifies first.
                raise LangfuseError(
                    f"HTTP {status}: the request may have arrived",
                    method=method,
                    path=full_path,
                    status=status,
                    delivery_unknown=True,
                )
            retriable = status == 429 or status >= 500
            if not retriable or last:
                reason = f"HTTP {status}" + (f" ({_AUTH_HINT})" if status in {401, 403} else "")
                raise LangfuseError(reason, method=method, path=full_path, status=status, retriable=retriable)
            self._backoff(attempt, response.headers.get("Retry-After"), method, full_path, f"HTTP {status}")
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: str | None, method: str, path: str, reason: str) -> None:
        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = self._settings.retry_backoff_seconds * 2**attempt
        logger.info("Langfuse %s %s: %s, retrying in %.1f s", method, path, reason, delay)
        self._sleep(delay)

    def _throttle(self) -> None:
        limit = self._settings.requests_per_minute
        now = self._clock()
        while self._sent and now - self._sent[0] >= _RATE_WINDOW_SECONDS:
            self._sent.popleft()
        if len(self._sent) >= limit:
            self._sleep(self._sent[0] + _RATE_WINDOW_SECONDS - now)
            now = self._clock()
            while self._sent and now - self._sent[0] >= _RATE_WINDOW_SECONDS:
                self._sent.popleft()
        self._sent.append(now)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None  # an HTTP date: fall back to the exponential backoff


def _body(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}
