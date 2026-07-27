"""Translate current LangGraph updates into typed observability events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agent_loop.events import EventEmitter

EVENT_METADATA_CONFIG_KEY = "_autobrowser_event_metadata"


def event_context_from_config(config: Mapping[str, Any] | None) -> dict[str, str | None]:
    """Extract event context carried through LangGraph config."""

    metadata = {}
    if isinstance(config, Mapping):
        metadata = dict(config.get(EVENT_METADATA_CONFIG_KEY) or {})
        public_metadata = config.get("metadata")
        if isinstance(public_metadata, Mapping):
            metadata = {**public_metadata, **metadata}
    task_id = _optional_str(metadata.get("task_id"))
    goal_id = _optional_str(metadata.get("goal_id")) or task_id
    return {
        "session_id": _optional_str(metadata.get("session_id")),
        "task_id": task_id,
        "goal_id": goal_id,
    }


def emit_chunk_events(
    emitter: EventEmitter,
    chunk: Any,
    *,
    context: dict[str, str | None] | None = None,
) -> None:
    """Emit graph and derived agent events for one LangGraph update chunk."""

    if not isinstance(chunk, Mapping):
        return
    event_context = dict(context or {})
    for node_name, update in chunk.items():
        payload = {
            "node": str(node_name),
        }
        emitter.emit(
            "graph.node_started",
            source="harness.runtime",
            payload=payload,
            **event_context,
        )
        if isinstance(update, Mapping):
            payload["update"] = dict(update)
        else:
            payload["update"] = update
        emitter.emit(
            "graph.node_finished",
            source="harness.runtime",
            payload=payload,
            **event_context,
        )
        if isinstance(update, Mapping):
            if str(node_name) in {"plan", "agent"}:
                emitter.emit(
                    "model.responded",
                    source="harness.runtime",
                    payload={"node": str(node_name)},
                    **event_context,
                )
            _emit_derived_events(emitter, str(node_name), update, event_context)


def _emit_derived_events(
    emitter: EventEmitter,
    node_name: str,
    update: Mapping[str, Any],
    context: dict[str, str | None],
) -> None:
    if node_name == "agent" and update.get("decision") == "tool_call":
        emitter.emit(
            "action.proposed",
            source="harness.runtime",
            payload={"tool_request": dict(update.get("tool_request") or {})},
            **context,
        )
    if node_name == "policy" and update.get("policy_event"):
        emitter.emit(
            "policy.decided",
            source="harness.runtime",
            payload=dict(update.get("policy_event") or {}),
            **context,
        )
    if node_name == "human_input" and update.get("policy_event"):
        emitter.emit(
            "approval.requested",
            source="harness.runtime",
            payload=dict(update.get("policy_event") or {}),
            **context,
        )
    if node_name == "executor" and update.get("tool_result"):
        emitter.emit(
            "tool.started",
            source="harness.runtime",
            payload={"tool_result": dict(update.get("tool_result") or {})},
            **context,
        )
        emitter.emit(
            "tool.finished",
            source="harness.runtime",
            payload={"tool_result": dict(update.get("tool_result") or {})},
            **context,
        )
    if node_name == "observe" and (
        update.get("observation") or update.get("snapshot")
    ):
        emitter.emit(
            "observation.compiled",
            source="harness.runtime",
            payload={
                "observation": update.get("observation", ""),
                "has_snapshot": bool(str(update.get("snapshot", "") or "").strip()),
            },
            **context,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "EVENT_METADATA_CONFIG_KEY",
    "emit_chunk_events",
    "event_context_from_config",
]
