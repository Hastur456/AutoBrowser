"""Telemetry boundary for harness runtime events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class TraceContext:
    """Minimal trace metadata returned by the telemetry boundary."""

    task_name: str
    started_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class TelemetryObserver:
    """Log harness events and provide a future LangSmith integration point."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("autobrowser.telemetry")

    def start_trace(
        self,
        task_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> TraceContext:
        """Start a trace for a task and return its local context."""

        trace = TraceContext(
            task_name=task_name,
            started_at=datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        self._logger.info(
            "Starting trace for task: %s",
            task_name,
            extra={"task_name": task_name, "metadata": trace.metadata},
        )
        return trace

    def log_error(
        self,
        exception: BaseException,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log an exception with traceback data when available."""

        self._logger.error(
            "Harness error: %s",
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
            extra={"metadata": dict(metadata or {})},
        )


__all__ = ["TelemetryObserver", "TraceContext"]
