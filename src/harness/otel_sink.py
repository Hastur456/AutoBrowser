"""OpenTelemetry trace export of a session's events over OTLP/HTTP.

:class:`OtlpEventSink` is a session event sink: it collects one task's events from
``goal.started`` to its terminal event, turns them into a span tree with
``src/harness/otel_spans.py`` and materializes the tree through the OpenTelemetry SDK with
the events' own timestamps. Each task is one trace, built and exported exactly once.
``session.closed`` ends a task that never finished, flushes the export within
``telemetry.otlp_timeout_seconds`` and shuts the provider down (a batch builds a session per
scenario, so exporter threads must not accumulate).

The OTLP exporter is an optional dependency group (``uv sync --group otel``) and is imported
only when export is enabled. The backend is chosen by ``telemetry.otlp_endpoint`` and
``telemetry.otlp_headers`` alone; nothing here knows a vendor, and header values are never
logged. See docs/decisions/2026-10-29-otlp-trace-export.md.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.agent_loop.events import EventRecord
from src.config import TelemetrySettings
from src.harness.otel_spans import AGENT_NAME, TERMINAL_EVENTS, SpanContext, SpanSpec, task_span_specs
from src.version import package_version

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

logger = logging.getLogger(__name__)

#: The module of the OTLP/HTTP span exporter (package ``opentelemetry-exporter-otlp-proto-http``).
OTLP_EXPORTER_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
MISSING_EXPORTER_MESSAGE = (
    "telemetry.otlp_enabled is set but the OTLP exporter is not installed: "
    "install with `uv sync --group otel`."
)


@dataclass(frozen=True)
class ModelIdentity:
    """The session's chat model as span attributes; read when a task starts."""

    model: str | None = None
    provider: str | None = None
    #: The model of a ``phase`` that does not use the session model (the approval judge).
    phase_models: Mapping[str, str] = field(default_factory=dict)


def otlp_exporter_installed() -> bool:
    """Whether the OTLP/HTTP exporter package can be imported."""

    try:
        importlib.import_module(OTLP_EXPORTER_MODULE)
    except ImportError:
        return False
    return True


def load_otlp_exporter(settings: TelemetrySettings) -> Any:
    """The OTLP/HTTP span exporter for ``settings``; a clear error without the package."""

    try:
        module = importlib.import_module(OTLP_EXPORTER_MODULE)
    except ImportError as exc:
        raise RuntimeError(MISSING_EXPORTER_MESSAGE) from exc
    return module.OTLPSpanExporter(
        endpoint=settings.otlp_endpoint,
        headers={name: value.get_secret_value() for name, value in settings.otlp_headers.items()},
        timeout=settings.otlp_timeout_seconds,
    )


def build_otlp_sink(
    settings: TelemetrySettings,
    *,
    models: Callable[[], ModelIdentity],
    processor: SpanProcessor | None = None,
) -> OtlpEventSink:
    """A sink over its own ``TracerProvider``; ``processor`` defaults to a background batch
    processor over the OTLP exporter (tests pass one over an in-memory exporter)."""

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if processor is None:
        timeout_ms = int(settings.otlp_timeout_seconds * 1000)
        processor = BatchSpanProcessor(load_otlp_exporter(settings), export_timeout_millis=timeout_ms)
    provider = TracerProvider(
        resource=Resource(
            {"service.name": settings.service_name, "service.version": package_version() or "unknown"}
        )
    )
    provider.add_span_processor(processor)
    return OtlpEventSink(settings, provider, models=models)


class OtlpEventSink:
    """Export each task of a session as one trace (see the module docstring)."""

    def __init__(
        self,
        settings: TelemetrySettings,
        provider: TracerProvider,
        *,
        models: Callable[[], ModelIdentity],
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._tracer = provider.get_tracer(AGENT_NAME, package_version() or None)
        self._models = models
        self._session_id: str | None = None
        self._labels: dict[str, str] = {}
        self._task: list[EventRecord] | None = None
        self._task_models = ModelIdentity()
        self._closed = False
        self._warned = False

    def emit(self, record: EventRecord) -> None:
        """Never raises: the first failure is logged (its type only), the rest are dropped."""

        if self._closed:
            return
        try:
            self._handle(record)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a session
            if not self._warned:
                self._warned = True
                logger.warning(
                    "OTLP trace export failed (%s); further export errors are dropped.",
                    type(exc).__name__,
                )

    def _handle(self, record: EventRecord) -> None:
        if record.type == "session.started":
            self._session_id = str(record.payload.get("session_id") or record.session_id or "") or None
            labels = record.payload.get("labels")
            if isinstance(labels, Mapping):
                self._labels = {str(key): str(value) for key, value in labels.items()}
        elif record.type == "session.closed":
            self._close()
        elif record.type == "goal.started":
            if self._task is not None:
                self._export_task()
            self._task = [record]
            self._task_models = self._models()
        elif self._task is not None and record.task_id in (None, self._task[0].task_id):
            self._task.append(record)
            if record.type in TERMINAL_EVENTS:
                self._export_task()

    def _close(self) -> None:
        self._closed = True
        try:
            if self._task is not None:
                self._export_task()
        finally:
            self._provider.force_flush(int(self._settings.otlp_timeout_seconds * 1000))
            self._provider.shutdown()

    def _export_task(self) -> None:
        events, self._task = self._task or [], None
        models = self._task_models
        context = SpanContext(
            session_id=self._session_id,
            labels=self._labels,
            environment=self._settings.environment,
            model=models.model,
            provider=models.provider,
            phase_models=models.phase_models,
            attribute_aliases=self._settings.attribute_aliases,
        )
        self._materialize(
            task_span_specs(events, capture_content=self._settings.capture_content, context=context)
        )

    def _materialize(self, specs: list[SpanSpec]) -> None:
        """Start every span (parents first) with its own times, then end each exactly once."""

        from opentelemetry import trace
        from opentelemetry.context import Context
        from opentelemetry.trace import SpanKind, Status, StatusCode

        spans: dict[str, trace.Span] = {}
        try:
            for spec in specs:
                parent = spans.get(spec.parent_key or "")
                span = self._tracer.start_span(
                    spec.name,
                    context=trace.set_span_in_context(parent) if parent is not None else Context(),
                    kind=SpanKind.INTERNAL,
                    attributes=dict(spec.attributes),
                    start_time=spec.start_ns,
                )
                spans[spec.key] = span
                for event in spec.events:
                    span.add_event(event.name, dict(event.attributes), timestamp=event.time_ns)
                if spec.error is not None:
                    span.set_status(Status(StatusCode.ERROR, spec.error))
        finally:
            for spec in reversed(specs):
                span = spans.get(spec.key)
                if span is not None:
                    span.end(end_time=spec.end_ns)
