"""OpenTelemetry trace export of a session's events over OTLP/HTTP.

The OTLP exporter is an optional dependency group (``uv sync --group otel``) and is imported
only when export is enabled. The backend is chosen by ``telemetry.otlp_endpoint`` and
``telemetry.otlp_headers`` alone; nothing here knows a vendor. See
docs/decisions/2026-10-29-otlp-trace-export.md.
"""

from __future__ import annotations

import importlib
from typing import Any

from src.config import TelemetrySettings

#: The module of the OTLP/HTTP span exporter (package ``opentelemetry-exporter-otlp-proto-http``).
OTLP_EXPORTER_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
MISSING_EXPORTER_MESSAGE = (
    "telemetry.otlp_enabled is set but the OTLP exporter is not installed: "
    "install with `uv sync --group otel`."
)


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
