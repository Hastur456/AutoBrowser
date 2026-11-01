"""An in-memory Langfuse for the bridge tests: no network, no real server.

``FakeLangfuse`` keeps datasets, versioned dataset items, OTLP/JSON traces, scores (upserted
by ``id``), score configs and annotation queues, and answers the public API paths the bridge
uses, following the OpenAPI spec of 2026-10-31. ``transport()`` gives an
``httpx.MockTransport``; ``serve()`` runs the same handler on a loopback HTTP server for a
real-socket test. ``fail(...)`` injects a status code or a transport exception into the next
matching requests.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

import httpx

API = "/api/public"
PUBLIC_KEY = "pk-lf-test-public"
SECRET_KEY = "sk-lf-test-secret"
PROJECT_ID = "project-test"


@dataclass
class Fault:
    method: str
    path: str
    status: int | None = None
    exception: type[Exception] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    times: int = 1
    #: For an exception: let the request take effect first (a timeout after the server got it).
    after_effect: bool = False
    #: Matching requests to let through untouched before the fault starts.
    skip: int = 0


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: Any


class FakeLangfuse:
    def __init__(self, *, version: str = "4.60.1", project_id: str = PROJECT_ID) -> None:
        self.version = version
        self.project_id = project_id
        self.datasets: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.item_versions: dict[str, list[dict[str, Any]]] = {}
        self.traces: list[dict[str, Any]] = []
        self.scores: dict[str, dict[str, Any]] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.queues: dict[str, dict[str, Any]] = {}
        self.queue_items: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[RecordedRequest] = []
        self.faults: list[Fault] = []
        self.page_size = 50
        self._ids = itertools.count(1)
        self._clock = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)

    # -- test helpers ---------------------------------------------------------------------------

    def fail(
        self,
        method: str,
        path: str,
        *,
        status: int | None = None,
        exception: type[Exception] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        times: int = 1,
        after_effect: bool = False,
        skip: int = 0,
    ) -> None:
        self.faults.append(Fault(method, path, status, exception, headers or {}, body, times, after_effect, skip))

    def writes(self) -> list[RecordedRequest]:
        return [request for request in self.requests if request.method in {"POST", "PATCH", "PUT", "DELETE"}]

    def spans(self) -> list[dict[str, Any]]:
        return [
            span
            for payload in self.traces
            for resource in payload.get("resourceSpans", [])
            for scope in resource.get("scopeSpans", [])
            for span in scope.get("spans", [])
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @contextlib.contextmanager
    def serve(self) -> Iterator[str]:
        """Serve the fake on 127.0.0.1 and yield its base URL."""

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = httpx.Request(
                    self.command,
                    f"http://{self.headers.get('Host')}{self.path}",
                    headers=dict(self.headers.items()),
                    content=body,
                )
                response = fake.handle(request)
                self.send_response(response.status_code)
                for name, value in response.headers.items():
                    if name.lower() not in {"content-length", "transfer-encoding"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = do_PATCH = _forward

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()

    def add_score(self, **score: Any) -> dict[str, Any]:
        """A score written by someone else (a judge or a human)."""

        score.setdefault("id", f"score-{next(self._ids)}")
        score.setdefault("timestamp", self._now())
        self.scores[score["id"]] = score
        return score

    # -- dispatch -------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            RecordedRequest(request.method, path, dict(request.url.params), dict(request.headers), body)
        )
        fault = self._fault(request.method, path)
        if fault is not None and not fault.after_effect:
            return self._fault_response(fault, request)
        if not self._authorized(request):
            return _json(401, {"message": "Invalid credentials"})
        response = self._route(request.method, path.removeprefix(API), dict(request.url.params), body)
        if fault is not None:
            return self._fault_response(fault, request)
        return response

    def _fault(self, method: str, path: str) -> Fault | None:
        for fault in self.faults:
            if fault.method == method and path.removeprefix(API).startswith(fault.path) and fault.times > 0:
                if fault.skip > 0:
                    fault.skip -= 1
                    return None
                fault.times -= 1
                return fault
        return None

    def _fault_response(self, fault: Fault, request: httpx.Request) -> httpx.Response:
        if fault.exception is not None:
            raise fault.exception("injected fault", request=request)  # type: ignore[call-arg]
        return _json(fault.status or 500, fault.body if fault.body is not None else {"message": "fault"}, fault.headers)

    def _authorized(self, request: httpx.Request) -> bool:
        if request.url.path == f"{API}/health":
            return True
        expected = base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
        return request.headers.get("authorization") == f"Basic {expected}"

    def _route(self, method: str, path: str, params: dict[str, str], body: Any) -> httpx.Response:
        if (method, path) == ("GET", "/health"):
            return _json(200, {"status": "OK", "version": self.version})
        if (method, path) == ("GET", "/projects"):
            return _json(200, {"data": [{"id": self.project_id, "name": "test", "organization": {}, "metadata": {}}]})
        if path == "/v2/datasets" and method == "POST":
            return self._create_dataset(body)
        if path.startswith("/v2/datasets/") and method == "GET":
            dataset = self.datasets.get(unquote(path.removeprefix("/v2/datasets/")))
            return _json(200, dataset) if dataset else _json(404, {"message": "Dataset not found"})
        if path == "/dataset-items" and method == "POST":
            return self._upsert_item(body)
        if path == "/dataset-items" and method == "GET":
            items = [item for item in self.items.values() if item["datasetName"] == params.get("datasetName")]
            return self._paged(items, params)
        if path == "/otel/v1/traces" and method == "POST":
            self.traces.append(body)
            return _json(200, {})
        if path == "/v2/observations" and method == "GET":
            spans = [span for span in self.spans() if span["traceId"] == params.get("traceId")]
            return _json(200, {"data": [{"id": span["spanId"], "traceId": span["traceId"]} for span in spans][: int(params.get("limit", 50))], "meta": {}})
        if path == "/scores" and method == "POST":
            return self._post_scores(body)
        if path == "/v3/scores" and method == "GET":
            return self._get_scores(params)
        if path == "/score-configs" and method == "GET":
            return self._paged(list(self.configs.values()), params)
        if path == "/score-configs" and method == "POST":
            config = {**body, "id": f"config-{next(self._ids)}", "isArchived": False, "projectId": self.project_id}
            self.configs[config["id"]] = config
            return _json(200, config)
        if path.startswith("/score-configs/") and method == "PATCH":
            config = self.configs[path.removeprefix("/score-configs/")]
            config.update(body)
            return _json(200, config)
        if path == "/annotation-queues" and method == "GET":
            return self._paged(list(self.queues.values()), params)
        if path == "/annotation-queues" and method == "POST":
            queue = {**body, "id": f"queue-{next(self._ids)}"}
            self.queues[queue["id"]] = queue
            return _json(200, queue)
        if path.startswith("/annotation-queues/") and path.endswith("/items") and method == "POST":
            queue_id = path.split("/")[2]
            item = {**body, "id": f"qitem-{next(self._ids)}", "queueId": queue_id}
            self.queue_items.setdefault(queue_id, []).append(item)
            return _json(200, item)
        return _json(404, {"message": f"no route {method} {path}"})

    # -- resources ------------------------------------------------------------------------------

    def _now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def _create_dataset(self, body: dict[str, Any]) -> httpx.Response:
        dataset = self.datasets.get(body["name"])
        if dataset is None:
            dataset = {"id": f"dataset-{next(self._ids)}", "name": body["name"], "projectId": self.project_id, "createdAt": self._now()}
            self.datasets[body["name"]] = dataset
        dataset.update({"description": body.get("description"), "metadata": body.get("metadata"), "updatedAt": self._now()})
        return _json(200, dataset)

    def _upsert_item(self, body: dict[str, Any]) -> httpx.Response:
        dataset = self.datasets.get(body["datasetName"])
        if dataset is None:
            return _json(404, {"message": "Dataset not found"})
        item_id = body.get("id") or f"item-{next(self._ids)}"
        existing = self.items.get(item_id)
        if existing is not None and existing["datasetName"] != body["datasetName"]:
            return _json(400, {"message": "Dataset item id already used in another dataset"})
        version = self._now()
        item = {
            "id": item_id,
            "datasetName": body["datasetName"],
            "datasetId": dataset["id"],
            "input": body.get("input"),
            "expectedOutput": body.get("expectedOutput"),
            "metadata": body.get("metadata"),
            "status": body.get("status") or "ACTIVE",
            "createdAt": existing["createdAt"] if existing else version,
            "updatedAt": version,
            "version": version,
        }
        self.items[item_id] = item
        self.item_versions.setdefault(item_id, []).append(dict(item))
        return _json(200, item)

    def _post_scores(self, body: list[dict[str, Any]]) -> httpx.Response:
        errors = []
        accepted = 0
        for index, score in enumerate(body):
            config = self.configs.get(score.get("configId") or "")
            if config is not None and config["dataType"] == "CATEGORICAL":
                labels = {category["label"] for category in config.get("categories") or []}
                if score["value"] not in labels:
                    errors.append({"index": index, "id": score.get("id"), "message": f"value {score['value']!r} is not a category"})
                    continue
            self.scores[score.get("id") or f"score-{next(self._ids)}"] = score
            accepted += 1
        if errors:
            return _json(207, {"accepted": accepted, "rejected": len(errors), "errors": errors})
        return _json(202, {"message": "accepted"})

    def _get_scores(self, params: dict[str, str]) -> httpx.Response:
        scores = list(self.scores.values())
        if "experimentId" in params:
            scores = [score for score in scores if score.get("experimentId") == params["experimentId"] or score.get("datasetRunId") == params["experimentId"]]
        if "source" in params:
            sources = set(params["source"].split(","))
            scores = [score for score in scores if score.get("source", "API") in sources]
        limit = int(params.get("limit", self.page_size))
        start = int(params.get("cursor") or 0)
        page = scores[start : start + limit]
        cursor = str(start + limit) if start + limit < len(scores) else None
        return _json(200, {"data": page, "meta": {"limit": limit, "cursor": cursor}})

    def _paged(self, rows: list[dict[str, Any]], params: dict[str, str]) -> httpx.Response:
        limit = int(params.get("limit", self.page_size))
        page = int(params.get("page", 1))
        total_pages = max(1, -(-len(rows) // limit))
        data = rows[(page - 1) * limit : page * limit]
        return _json(200, {"data": data, "meta": {"page": page, "limit": limit, "totalItems": len(rows), "totalPages": total_pages}})


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})
