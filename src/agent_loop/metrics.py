"""Pure metrics extraction for task-scoped AutoBrowser event traces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.agent_loop.events import EventRecord

TERMINAL_EVENT_TYPES = {
    "goal.completed",
    "goal.failed",
    "goal.blocked",
    "goal.cancelled",
}


@dataclass(frozen=True)
class EventMetrics:
    """Derived metrics for one task's event records."""

    duration_ms: int | None
    terminal_status: str
    model_turn_count: int
    tool_call_count: int
    policy_block_count: int
    approval_request_count: int
    observation_count: int
    error_count: int
    final_answer: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible metrics dictionary."""

        return {
            "duration_ms": self.duration_ms,
            "terminal_status": self.terminal_status,
            "model_turn_count": self.model_turn_count,
            "tool_call_count": self.tool_call_count,
            "policy_block_count": self.policy_block_count,
            "approval_request_count": self.approval_request_count,
            "observation_count": self.observation_count,
            "error_count": self.error_count,
            "final_answer": self.final_answer,
        }


def extract_event_metrics(events: Iterable[EventRecord | Mapping[str, Any]]) -> EventMetrics:
    """Calculate export metrics from the events for a single task."""

    normalized_events = [_event_view(event) for event in events]
    terminal = _terminal_event(normalized_events)
    return EventMetrics(
        duration_ms=_duration_ms(normalized_events),
        terminal_status=_terminal_status(terminal),
        model_turn_count=_count_type(normalized_events, "model.responded"),
        tool_call_count=_count_type(normalized_events, "tool.finished"),
        policy_block_count=sum(
            1
            for event in normalized_events
            if event.type == "policy.decided" and _is_policy_block(event.payload)
        ),
        approval_request_count=_count_type(normalized_events, "approval.requested"),
        observation_count=_count_type(normalized_events, "observation.compiled"),
        error_count=sum(1 for event in normalized_events if _is_error_event(event)),
        final_answer=_final_answer(terminal),
    )


@dataclass(frozen=True)
class _EventView:
    type: str
    timestamp: datetime | None
    payload: Mapping[str, Any]


def _event_view(event: EventRecord | Mapping[str, Any]) -> _EventView:
    if isinstance(event, EventRecord):
        return _EventView(
            type=str(event.type),
            timestamp=event.timestamp,
            payload=event.payload,
        )
    return _EventView(
        type=str(event.get("type") or ""),
        timestamp=_parse_timestamp(event.get("timestamp")),
        payload=_mapping(event.get("payload")),
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _terminal_event(events: list[_EventView]) -> _EventView | None:
    for event in reversed(events):
        if event.type in TERMINAL_EVENT_TYPES:
            return event
    return None


def _terminal_status(event: _EventView | None) -> str:
    if event is None:
        return "missing"
    return event.type.removeprefix("goal.")


def _duration_ms(events: list[_EventView]) -> int | None:
    timestamps = [event.timestamp for event in events if event.timestamp is not None]
    if not timestamps:
        return None
    delta = max(timestamps) - min(timestamps)
    return max(0, int(delta.total_seconds() * 1000))


def _count_type(events: list[_EventView], event_type: str) -> int:
    return sum(1 for event in events if event.type == event_type)


def _is_policy_block(payload: Mapping[str, Any]) -> bool:
    decision = payload.get("decision")
    if isinstance(decision, str):
        return decision == "blocked"
    if isinstance(decision, Mapping):
        return decision.get("status") == "blocked" or decision.get("decision") == "blocked"
    return False


def _is_error_event(event: _EventView) -> bool:
    if event.type in {"graph.failed", "goal.failed"}:
        return True
    if event.type != "tool.finished":
        return False
    result = _mapping(event.payload.get("tool_result"))
    status = str(result.get("status") or "").lower()
    return bool(result.get("error")) or status in {"error", "failed", "failure"}


def _final_answer(event: _EventView | None) -> str:
    if event is None:
        return ""
    result = event.payload.get("result")
    answer = _find_final_answer(result)
    return "" if answer is None else str(answer)


def _find_final_answer(value: Any) -> Any | None:
    if isinstance(value, Mapping):
        if value.get("final_answer"):
            return value["final_answer"]
        for nested_value in value.values():
            nested_answer = _find_final_answer(nested_value)
            if nested_answer:
                return nested_answer
    return None


__all__ = [
    "EventMetrics",
    "extract_event_metrics",
]
