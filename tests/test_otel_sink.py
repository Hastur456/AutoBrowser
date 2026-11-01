"""``src/harness/otel_sink.py``: the OTLP exporter loader and the span-materializing sink.

The OTLP exporter package is an optional group (``uv sync --group otel``): these tests never
need it — they inject a fake exporter module or the SDK's ``InMemorySpanExporter``.
"""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from src.agent_loop.events import EventRecord
from src.config import TelemetrySettings
from src.harness import otel_sink
from src.harness.otel_sink import ModelIdentity, OtlpEventSink, build_otlp_sink
from src.harness.session import _telemetry_sinks
from tests.test_harness_session import make_config
from tests.test_otel_spans import TASK, ev, ns, trace

EXPORTER_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
HEADER_SECRET = "Basic aGVhZGVyLXNlY3JldA=="
TELEMETRY = TelemetrySettings(
    otlp_enabled=True,
    otlp_headers={"Authorization": SecretStr(HEADER_SECRET)},
    environment="interactive",
    attribute_aliases={"autobrowser.batch.id": "backend.batch"},
)
MODELS = ModelIdentity(model="gemma4:31b-cloud", provider="ollama")


# -- the exporter loader -------------------------------------------------------------------------


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


# -- the sink -------------------------------------------------------------------------------------


def session_started(labels: dict[str, str] | None = None) -> EventRecord:
    return EventRecord(
        type="session.started",
        source="test",
        payload={"session_id": "sess-1", "resumed": False, "labels": labels or {}},
        session_id="sess-1",
    )


def session_closed() -> EventRecord:
    return EventRecord(type="session.closed", source="test", payload={"session_id": "sess-1"}, session_id="sess-1")


def sink_with(processor: Any, settings: TelemetrySettings = TELEMETRY) -> OtlpEventSink:
    return build_otlp_sink(settings, models=lambda: MODELS, processor=processor)


def run(sink: OtlpEventSink, events: Sequence[EventRecord]) -> None:
    for event in events:
        sink.emit(event)


def test_a_task_is_exported_as_one_trace_of_spans() -> None:
    exporter = InMemorySpanExporter()

    run(sink_with(SimpleSpanProcessor(exporter)), [session_started({"autobrowser.batch.id": "b-1"}), *trace()])

    exported = exporter.get_finished_spans()
    spans = {span.name: span for span in exported}
    assert len(exported) == 5
    root = spans["invoke_agent autobrowser"]
    tool = spans["execute_tool click"]
    assert root.parent is None
    assert {span.context.trace_id for span in exported} == {root.context.trace_id}
    assert all(span.parent.span_id == root.context.span_id for span in exported if span is not root)
    assert (root.start_time, root.end_time) == (ns(0), ns(270))
    assert (tool.start_time, tool.end_time) == (ns(122), ns(222))
    for span in exported:
        assert span.attributes["session.id"] == "sess-1"
        assert span.attributes["autobrowser.batch.id"] == "b-1"
        assert span.attributes["backend.batch"] == "b-1"
        assert span.attributes["deployment.environment.name"] == "interactive"
    assert spans["chat gemma4:31b-cloud"].attributes["gen_ai.provider.name"] == "ollama"
    assert [event.name for event in root.events] == ["plan.updated"]
    assert root.resource.attributes["service.name"] == "autobrowser"


def test_the_model_is_read_when_the_task_starts() -> None:
    exporter = InMemorySpanExporter()
    current = [MODELS]
    sink = build_otlp_sink(TELEMETRY, models=lambda: current[0], processor=SimpleSpanProcessor(exporter))

    sink.emit(session_started())
    run(sink, trace()[:1])
    current[0] = ModelIdentity(model="other-model", provider="openai_compat")
    run(sink, trace()[1:])

    assert "chat gemma4:31b-cloud" in {span.name for span in exporter.get_finished_spans()}


def test_error_spans_carry_the_error_status() -> None:
    exporter = InMemorySpanExporter()
    events = trace(ev("goal.blocked", 270, {"status": "blocked", "stop_reason": "approval_denied"}))

    run(sink_with(SimpleSpanProcessor(exporter)), [session_started(), *events])

    root = next(span for span in exporter.get_finished_spans() if span.parent is None)
    assert root.status.status_code is StatusCode.ERROR
    assert root.status.description == "approval_denied"


def test_headers_never_reach_attributes_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    exporter = InMemorySpanExporter()
    caplog.set_level(logging.DEBUG)

    run(sink_with(SimpleSpanProcessor(exporter)), [session_started(), *trace(), session_closed()])

    values = [str(value) for span in exporter.get_finished_spans() for value in span.attributes.values()]
    assert not any(HEADER_SECRET in value or "Authorization" in value for value in values)
    assert not any(TASK in value for value in values)
    assert HEADER_SECRET not in caplog.text


class RaisingExporter(InMemorySpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("backend down")


class RaisingProcessor(SimpleSpanProcessor):
    def on_end(self, span: ReadableSpan) -> None:
        raise RuntimeError(f"processor down {HEADER_SECRET}")


def test_an_exporter_failure_does_not_propagate() -> None:
    run(sink_with(SimpleSpanProcessor(RaisingExporter())), [session_started(), *trace(), session_closed()])


def test_a_sink_failure_is_logged_once_without_its_message(caplog: pytest.LogCaptureFixture) -> None:
    sink = sink_with(RaisingProcessor(InMemorySpanExporter()))

    run(sink, [session_started(), *trace(), *trace(), session_closed()])

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == otel_sink.__name__]
    assert len(warnings) == 1
    assert "RuntimeError" in caplog.text
    assert HEADER_SECRET not in caplog.text


class SpyExporter(InMemorySpanExporter):
    shut_down = False

    def shutdown(self) -> None:
        self.shut_down = True
        super().shutdown()


def test_session_closed_flushes_then_shuts_the_exporter_down() -> None:
    exporter = SpyExporter()
    sink = sink_with(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))

    run(sink, [session_started(), *trace()])
    assert exporter.get_finished_spans() == ()

    sink.emit(session_closed())

    assert len(exporter.get_finished_spans()) == 5
    assert exporter.shut_down
    run(sink, trace())  # after close: ignored, no error
    assert len(exporter.get_finished_spans()) == 5


def test_no_span_is_exported_twice() -> None:
    exporter = InMemorySpanExporter()
    cancelled = ev("goal.cancelled", 300, {"status": "cancelled", "stop_reason": "cancelled"})
    unclosed = [*trace()[:8], cancelled]
    late = [ev("tool.finished", 310, {"tool_result": {"name": "click", "status": "success"}}), cancelled]
    open_task = trace()[:5]

    run(sink_with(SimpleSpanProcessor(exporter)), [session_started(), *unclosed, *late, *open_task, session_closed()])

    spans = exporter.get_finished_spans()
    span_ids = [span.context.span_id for span in spans]
    assert len(span_ids) == len(set(span_ids))
    # cancelled task: root, plan, agent, the unclosed tool; open task: root, plan, the unclosed agent
    assert len(spans) == 4 + 3
    incomplete = sorted(span.name for span in spans if span.attributes.get("autobrowser.incomplete"))
    assert incomplete == ["chat gemma4:31b-cloud", "execute_tool click", "invoke_agent autobrowser"]


# -- wiring ---------------------------------------------------------------------------------------


def test_the_session_builds_no_sink_by_default_or_without_otlp_export(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_exporter(_settings: TelemetrySettings) -> None:
        raise AssertionError("no sink may be built")

    monkeypatch.setattr(otel_sink, "load_otlp_exporter", no_exporter)
    config = make_config()

    assert config.otlp_export is True
    assert _telemetry_sinks(config, TelemetrySettings(), models=lambda: MODELS) == []
    assert _telemetry_sinks(replace(config, otlp_export=False), TELEMETRY, models=lambda: MODELS) == []


def test_the_session_builds_the_sink_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_sink, "load_otlp_exporter", lambda _settings: InMemorySpanExporter())

    sinks = _telemetry_sinks(make_config(), TELEMETRY, models=lambda: MODELS)

    assert len(sinks) == 1 and isinstance(sinks[0], OtlpEventSink)
    sinks[0].emit(session_closed())


def test_an_enabled_export_without_the_package_fails_the_session_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, EXPORTER_MODULE, None)

    with pytest.raises(RuntimeError, match="uv sync --group otel"):
        _telemetry_sinks(make_config(), TELEMETRY, models=lambda: MODELS)
