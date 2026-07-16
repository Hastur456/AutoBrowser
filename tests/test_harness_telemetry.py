from __future__ import annotations

import logging

from src.harness.telemetry import TelemetryObserver, TraceContext


def test_telemetry_observer_starts_trace(caplog) -> None:
    observer = TelemetryObserver()

    with caplog.at_level(logging.INFO, logger="autobrowser.telemetry"):
        trace = observer.start_trace("inspect page", metadata={"source": "test"})

    assert isinstance(trace, TraceContext)
    assert trace.task_name == "inspect page"
    assert trace.metadata == {"source": "test"}
    assert "Starting trace for task: inspect page" in caplog.text


def test_telemetry_observer_logs_error(caplog) -> None:
    observer = TelemetryObserver()
    error = RuntimeError("tool failed")

    with caplog.at_level(logging.ERROR, logger="autobrowser.telemetry"):
        observer.log_error(error, metadata={"node": "executor"})

    assert "Harness error: tool failed" in caplog.text
