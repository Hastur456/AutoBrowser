"""``src/harness/otel_sink.py``: the OTLP exporter loader and the span-materializing sink.

The OTLP exporter package is an optional group (``uv sync --group otel``): these tests never
need it — they inject a fake exporter module or the SDK's ``InMemorySpanExporter``.
"""

from __future__ import annotations

import sys
import types

import pytest
from pydantic import SecretStr

from src.config import TelemetrySettings
from src.harness import otel_sink

EXPORTER_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def test_a_missing_exporter_package_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, EXPORTER_MODULE, None)

    with pytest.raises(RuntimeError, match="uv sync --group otel"):
        otel_sink.load_otlp_exporter(TelemetrySettings(otlp_enabled=True))
    assert otel_sink.otlp_exporter_installed() is False


def test_the_exporter_gets_the_endpoint_headers_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, object] = {}

    class FakeExporter:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setitem(sys.modules, EXPORTER_MODULE, types.SimpleNamespace(OTLPSpanExporter=FakeExporter))
    settings = TelemetrySettings(
        otlp_enabled=True,
        otlp_endpoint="https://otel.test/v1/traces",
        otlp_headers={"Authorization": SecretStr("Basic abc")},
        otlp_timeout_seconds=7.0,
    )

    exporter = otel_sink.load_otlp_exporter(settings)

    assert isinstance(exporter, FakeExporter)
    assert built == {
        "endpoint": "https://otel.test/v1/traces",
        "headers": {"Authorization": "Basic abc"},
        "timeout": 7.0,
    }
    assert otel_sink.otlp_exporter_installed() is True
