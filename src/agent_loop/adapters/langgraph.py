"""Translate proposed actions back into legacy LangGraph state updates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.agent.state import AgentState
from src.agent.utils import _done_response, _replan_response, _tool_request_update
from src.harness.memory import ensure_message_history
from src.agent_loop.actions import ProposedAction, normalize_tool_request


def proposed_action_to_legacy_update(
    action: ProposedAction | Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    messages: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Convert a proposed action into the existing AgentState update shape."""

    kind = str(action.get("kind", "") or "")
    if kind == "answer":
        return _done_response(
            state,  # type: ignore[arg-type]
            str(action.get("final_answer", "") or ""),
            messages=list(messages) if messages is not None else None,
            reason=str(action.get("reason", "") or ""),
            action=kind,
        )

    if kind == "tool_call":
        request = normalize_tool_request(action.get("tool_request"))
        legacy_messages = _messages_or_history(state, messages)
        return _tool_request_update(
            state,  # type: ignore[arg-type]
            legacy_messages,
            request,
        )

    if kind == "update_plan":
        return _replan_response(
            str(action.get("reason", "") or "Replanning requested."),
        )

    if kind == "ask_user":
        question = str(action.get("question", "") or action.get("reason", "") or "")
        return _replan_response(question or "Human input requested.")

    if kind == "delegate":
        objective = str(action.get("objective", "") or "")
        role = str(action.get("role", "") or "")
        reason = str(action.get("reason", "") or "").strip()
        return _replan_response(
            reason
            or f"Delegation requested for {role or 'another agent'}: {objective}".strip()
        )

    if kind == "compact_memory":
        summary = str(action.get("summary", "") or "")
        return _replan_response(
            str(action.get("reason", "") or summary or "Memory compaction requested.")
        )

    if kind == "stop":
        message = str(action.get("message", "") or "")
        status = str(action.get("status", "") or "blocked")
        if status == "done":
            return _done_response(
                state,  # type: ignore[arg-type]
                message,
                messages=list(messages) if messages is not None else None,
                reason=str(action.get("reason", "") or ""),
                action=kind,
            )
        if status == "cancelled":
            return _done_response(
                state,  # type: ignore[arg-type]
                f"Cancelled: {message}" if message else "Cancelled.",
                messages=list(messages) if messages is not None else None,
                reason=str(action.get("reason", "") or ""),
                action=kind,
            )
        if status == "failed":
            return _done_response(
                state,  # type: ignore[arg-type]
                f"Failed: {message}" if message else "Failed.",
                messages=list(messages) if messages is not None else None,
                reason=str(action.get("reason", "") or ""),
                action=kind,
            )
        return _done_response(
            state,  # type: ignore[arg-type]
            f"Blocked: {message}" if message else "Blocked.",
            messages=list(messages) if messages is not None else None,
            reason=str(action.get("reason", "") or ""),
            action=kind,
        )

    return _replan_response("Unrecognized proposed action.")


def _messages_or_history(
    state: Mapping[str, Any],
    messages: Sequence[Any] | None,
) -> list[Any]:
    if messages is not None:
        return list(messages)
    return ensure_message_history(state)


__all__ = ["proposed_action_to_legacy_update"]
