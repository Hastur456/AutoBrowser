"""``scripts/langfuse_bridge/client.py``: the only place that knows the Langfuse API.

Against ``tests/platform_fixtures/fake_langfuse.py`` through ``httpx.MockTransport``: no
network. Settings are built explicitly, never from ``get_settings()``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from scripts.langfuse_bridge.client import LangfuseClient, LangfuseError
from src.config import EvalPlatformSettings
from tests.platform_fixtures.fake_langfuse import PUBLIC_KEY, SECRET_KEY, FakeLangfuse

HOST = "https://platform.example.test"


def settings(**overrides: Any) -> EvalPlatformSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "host": HOST,
        "public_key": SecretStr(PUBLIC_KEY),
        "secret_key": SecretStr(SECRET_KEY),
        "max_retries": 2,
        "retry_backoff_seconds": 1.0,
        "requests_per_minute": 1000,
    }
    values.update(overrides)
    return EvalPlatformSettings(**values)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client(fake: FakeLangfuse, clock: Clock | None = None, **overrides: Any) -> LangfuseClient:
    clock = clock or Clock()
    return LangfuseClient(settings(**overrides), transport=fake.transport(), clock=clock, sleep=clock.sleep)


def test_requests_carry_basic_auth_and_the_otlp_export_the_ingestion_version() -> None:
    fake = FakeLangfuse()
    api = client(fake)

    api.project()
    api.export_otlp({"resourceSpans": []})

    expected = "Basic " + base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
    assert all(request.headers["authorization"] == expected for request in fake.requests)
    otlp = fake.requests[-1]
    assert otlp.path == "/api/public/otel/v1/traces"
    assert otlp.headers["x-langfuse-ingestion-version"] == "4"
    assert otlp.headers["content-type"] == "application/json"


def test_health_and_project_and_the_version_check() -> None:
    api = client(FakeLangfuse(version="4.59.0"))

    health = api.health()

    assert health.version == "4.59.0" and health.compatible
    assert api.project().id == "project-test"
    assert not client(FakeLangfuse(version="3.120.0")).health().compatible
    assert not client(FakeLangfuse(version="v4.58.9-rc1")).health().compatible
    assert client(FakeLangfuse(version="v4.60.0")).health().compatible


def test_a_client_without_host_or_keys_is_a_configuration_error() -> None:
    with pytest.raises(LangfuseError, match="eval_platform.host"):
        LangfuseClient(settings(host=None))
    with pytest.raises(LangfuseError, match="eval_platform.public_key"):
        LangfuseClient(settings(secret_key=None))


def test_429_waits_for_retry_after_then_succeeds() -> None:
    fake = FakeLangfuse()
    fake.fail("GET", "/projects", status=429, headers={"Retry-After": "7"})
    clock = Clock()

    assert client(fake, clock).project().id == "project-test"

    assert clock.sleeps == [7.0]
    assert len(fake.requests) == 2


def test_5xx_and_network_errors_retry_with_exponential_backoff() -> None:
    fake = FakeLangfuse()
    fake.fail("GET", "/projects", status=503)
    fake.fail("GET", "/projects", exception=httpx.ConnectError)
    clock = Clock()

    client(fake, clock).project()

    assert clock.sleeps == [1.0, 2.0]


def test_retries_are_bounded() -> None:
    fake = FakeLangfuse()
    fake.fail("GET", "/projects", status=502, times=10)

    with pytest.raises(LangfuseError) as raised:
        client(fake).project()

    assert raised.value.status == 502 and raised.value.retriable
    assert len(fake.requests) == 3  # max_retries=2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_is_not_retried(status: int) -> None:
    fake = FakeLangfuse()
    fake.fail("GET", "/projects", status=status)

    with pytest.raises(LangfuseError) as raised:
        client(fake).project()

    assert raised.value.status == status and not raised.value.retriable
    assert len(fake.requests) == 1
    if status in {401, 403}:
        assert "eval_platform.public_key/secret_key" in str(raised.value)


def test_requests_are_throttled_to_the_configured_rate() -> None:
    fake = FakeLangfuse()
    clock = Clock()
    api = client(fake, clock, requests_per_minute=2)

    for _ in range(3):
        api.project()

    assert clock.sleeps == [60.0]


def test_the_otlp_export_is_not_retried_after_the_request_may_have_arrived() -> None:
    fake = FakeLangfuse()
    fake.fail("POST", "/otel/v1/traces", exception=httpx.ReadTimeout, after_effect=True)

    with pytest.raises(LangfuseError) as raised:
        client(fake).export_otlp({"resourceSpans": []})

    assert raised.value.delivery_unknown
    assert len(fake.traces) == 1 and len(fake.requests) == 1


@pytest.mark.parametrize("status", [500, 502, 504])
def test_an_otlp_server_error_may_have_been_ingested_and_is_not_retried(status: int) -> None:
    fake = FakeLangfuse()
    fake.fail("POST", "/otel/v1/traces", status=status, times=5)

    with pytest.raises(LangfuseError) as raised:
        client(fake).export_otlp({"resourceSpans": []})

    assert raised.value.delivery_unknown and raised.value.status == status
    assert len(fake.requests) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_an_otlp_export_retries_a_request_the_server_did_not_take(status: int) -> None:
    fake = FakeLangfuse()
    fake.fail("POST", "/otel/v1/traces", status=status)

    client(fake).export_otlp({"resourceSpans": []})

    assert len(fake.traces) == 1


def test_the_otlp_export_retries_a_refused_connection() -> None:
    fake = FakeLangfuse()
    fake.fail("POST", "/otel/v1/traces", exception=httpx.ConnectError)

    client(fake).export_otlp({"resourceSpans": []})

    assert len(fake.traces) == 1


def test_a_partial_score_batch_is_reported_and_not_retried() -> None:
    fake = FakeLangfuse()
    api = client(fake)
    config_id = api.create_score_config({"name": "kind", "dataType": "CATEGORICAL", "categories": [{"label": "a", "value": 0}]})

    result = api.post_scores([
        {"id": "s1", "name": "kind", "value": "a", "dataType": "CATEGORICAL", "configId": config_id},
        {"id": "s2", "name": "kind", "value": "zzz", "dataType": "CATEGORICAL", "configId": config_id},
    ])

    assert (result.accepted, result.rejected) == (1, 1)
    assert "zzz" in result.errors[0]["message"]
    assert len([request for request in fake.requests if request.path.endswith("/scores")]) == 1
    assert (api.post_scores([{"id": "s3", "name": "n", "value": 1, "dataType": "NUMERIC"}]).accepted, len(fake.scores)) == (1, 2)


def test_cursor_pagination_reads_every_score() -> None:
    fake = FakeLangfuse()
    for index in range(5):
        fake.add_score(id=f"j{index}", name="judge.fields", value=1, source="EVAL", experimentId="batch-1")
    fake.add_score(id="other", name="x", value=1, source="API", experimentId="batch-1")

    found = list(client(fake).scores(experimentId="batch-1", source="EVAL,ANNOTATION", limit="2"))

    assert [score["id"] for score in found] == [f"j{index}" for index in range(5)]


def test_page_pagination_reads_every_dataset_item_and_config() -> None:
    fake = FakeLangfuse()
    fake.page_size = 2
    api = client(fake)
    api.ensure_dataset("autobrowser/golden", {"tasks_sha256": "h"})
    for index in range(5):
        api.upsert_dataset_item({"datasetName": "autobrowser/golden", "id": f"i{index}", "input": {}})
    for index in range(3):
        api.create_score_config({"name": f"c{index}", "dataType": "NUMERIC"})

    assert [item["id"] for item in api.dataset_items("autobrowser/golden")] == [f"i{index}" for index in range(5)]
    assert [config["name"] for config in api.score_configs()] == ["c0", "c1", "c2"]


def test_ensure_dataset_creates_once_and_updates_only_changed_metadata() -> None:
    fake = FakeLangfuse()
    api = client(fake)

    first = api.ensure_dataset("autobrowser/golden", {"tasks_sha256": "h1"})
    writes = len(fake.writes())
    same = api.ensure_dataset("autobrowser/golden", {"tasks_sha256": "h1"})
    unchanged_writes = len(fake.writes())
    api.ensure_dataset("autobrowser/golden", {"tasks_sha256": "h2"})

    assert first == same
    assert (writes, unchanged_writes, len(fake.writes())) == (1, 1, 2)
    assert fake.datasets["autobrowser/golden"]["metadata"] == {"tasks_sha256": "h2"}
    assert any(request.path.endswith("/v2/datasets/autobrowser%2Fgolden") or request.path.endswith("/v2/datasets/autobrowser/golden") for request in fake.requests)


def test_trace_exists_reads_the_observations() -> None:
    fake = FakeLangfuse()
    api = client(fake)
    api.export_otlp({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "spanId": "cd" * 8}]}]}]})

    assert api.trace_exists("ab" * 16)
    assert not api.trace_exists("ef" * 16)


def test_queues_and_score_config_archiving() -> None:
    fake = FakeLangfuse()
    api = client(fake)
    config_id = api.create_score_config({"name": "human.verdict", "dataType": "CATEGORICAL", "categories": []})

    queue = api.ensure_queue("triage", [config_id])
    assert api.ensure_queue("triage", [config_id]) == queue
    api.enqueue(queue, "ab" * 16)
    api.archive_score_config(config_id)

    assert fake.queue_items[queue] == [{"objectId": "ab" * 16, "objectType": "TRACE", "id": fake.queue_items[queue][0]["id"], "queueId": queue}]
    assert fake.configs[config_id]["isArchived"] is True
    assert len([q for q in fake.queues.values() if q["name"] == "triage"]) == 1


def test_errors_and_logs_never_hold_the_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeLangfuse()
    fake.fail("GET", "/projects", status=401)
    fake.fail("GET", "/health", status=500, times=10)

    errors = []
    for call in (client(fake).project, client(fake).health):
        with pytest.raises(LangfuseError) as raised:
            call()
        errors.append(raised.value)

    text = " ".join([str(error) for error in errors] + [repr(error) for error in errors] + [caplog.text])
    for secret in (SECRET_KEY, PUBLIC_KEY, base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()):
        assert secret not in text
    assert "GET /api/public/projects" in str(errors[0])
