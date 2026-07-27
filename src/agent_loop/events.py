"""Typed event records and durable sinks for agent-loop observability."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

EventType = Literal[
    "session.started",
    "session.closed",
    "goal.started",
    "graph.started",
    "graph.node_started",
    "graph.node_finished",
    "graph.failed",
    "model.requested",
    "model.responded",
    "action.proposed",
    "policy.decided",
    "approval.requested",
    "tool.started",
    "tool.finished",
    "observation.compiled",
    "goal.completed",
    "goal.blocked",
    "goal.failed",
    "goal.cancelled",
]

SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "authorization",
)
REDACTED_VALUE = "[REDACTED]"
MAX_STRING_CHARS = 20_000


@dataclass(frozen=True)
class EventRecord:
    """One durable event emitted by the AutoBrowser runtime."""

    type: EventType
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"event-{uuid4().hex}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    goal_id: str | None = None
    task_id: str | None = None
    parent_id: str | None = None
    sequence: int = 0

    def to_json_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-compatible representation."""

        return {
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "sequence": self.sequence,
            "source": self.source,
            "payload": redact_json_safe(self.payload),
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "EventRecord":
        """Build an event record from a JSONL dictionary."""

        raw_timestamp = data.get("timestamp")
        timestamp = (
            datetime.fromisoformat(raw_timestamp)
            if isinstance(raw_timestamp, str)
            else datetime.now(UTC)
        )
        return cls(
            event_id=str(data.get("event_id") or f"event-{uuid4().hex}"),
            type=str(data.get("type") or "graph.started"),  # type: ignore[arg-type]
            timestamp=timestamp,
            session_id=_optional_str(data.get("session_id")),
            goal_id=_optional_str(data.get("goal_id")),
            task_id=_optional_str(data.get("task_id")),
            parent_id=_optional_str(data.get("parent_id")),
            sequence=int(data.get("sequence", 0) or 0),
            source=str(data.get("source") or ""),
            payload=dict(data.get("payload") or {}),
        )


class EventSink(Protocol):
    """Destination for typed event records."""

    def emit(self, record: EventRecord) -> None:
        """Persist or store one event record."""


class NullEventSink:
    """Event sink that discards all records."""

    def emit(self, record: EventRecord) -> None:
        _ = record


class InMemoryEventSink:
    """Event sink for deterministic tests."""

    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    def emit(self, record: EventRecord) -> None:
        self.records.append(record)


class JsonlEventSink:
    """Append typed event records to a JSONL trace file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: EventRecord) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.to_json_dict(), ensure_ascii=False))
            file.write("\n")
            file.flush()


class CompositeEventSink:
    """Fan out records to multiple event sinks."""

    def __init__(self, sinks: list[EventSink] | None = None) -> None:
        self._sinks = list(sinks or [])

    def emit(self, record: EventRecord) -> None:
        for sink in self._sinks:
            sink.emit(record)


class EventEmitter:
    """Create typed events with shared context and monotonic sequence numbers."""

    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        self.sink = sink or NullEventSink()
        self.session_id = session_id
        self._sequence = 0

    def emit(
        self,
        event_type: EventType,
        *,
        source: str,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        goal_id: str | None = None,
        task_id: str | None = None,
        parent_id: str | None = None,
    ) -> EventRecord:
        self._sequence += 1
        record = EventRecord(
            type=event_type,
            source=source,
            payload=dict(payload or {}),
            session_id=session_id or self.session_id,
            goal_id=goal_id,
            task_id=task_id,
            parent_id=parent_id,
            sequence=self._sequence,
        )
        self.sink.emit(record)
        return record


def redact_json_safe(value: Any, *, key: str | None = None) -> Any:
    """Return a redacted JSON-compatible value."""

    if key is not None and _is_sensitive_key(key):
        return REDACTED_VALUE
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    if isinstance(value, dict):
        return {
            str(item_key): redact_json_safe(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_json_safe(item) for item in value]
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}... [truncated]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "CompositeEventSink",
    "EventEmitter",
    "EventRecord",
    "EventSink",
    "EventType",
    "InMemoryEventSink",
    "JsonlEventSink",
    "NullEventSink",
    "REDACTED_VALUE",
    "redact_json_safe",
]
