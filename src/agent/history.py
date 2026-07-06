"""Conversation history helpers for the AutoBrowser agent."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.agent.prompts import AGENT_SYSTEM_PROMPT
from src.agent.state import AgentState, CompactToolObservation, ToolRequest, ToolResult

MAX_TOOL_MESSAGE_REFS = 25


def ensure_message_history(state: AgentState) -> list[BaseMessage]:
    """Return existing history or initialize it with system and original user task."""

    messages = list(state.get("messages") or [])
    task = str(state.get("task", "") or "Complete the task.").strip()
    if not any(message.type == "system" for message in messages):
        messages.insert(0, SystemMessage(content=AGENT_SYSTEM_PROMPT))
    if not any(
        message.type == "human"
        and str(message.content).startswith("Original user request:\n")
        for message in messages
    ):
        insert_at = 1 if messages and messages[0].type == "system" else 0
        messages.insert(insert_at, HumanMessage(content=f"Original user request:\n{task}"))
    return messages


def with_tool_call_id(request: ToolRequest) -> ToolRequest:
    """Return a copy of a tool request with a stable chat tool-call id."""

    updated: ToolRequest = {
        "name": str(request.get("name", "") or ""),
        "args": request.get("args") if isinstance(request.get("args"), dict) else {},
        "reason": str(request.get("reason", "") or ""),
        "id": str(request.get("id", "") or f"call_{uuid4().hex}"),
    }
    return updated


def append_ai_tool_call(
    messages: list[BaseMessage],
    request: ToolRequest,
) -> list[BaseMessage]:
    """Append the assistant tool call selected by the reasoning LLM."""

    return [
        *messages,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": str(request.get("name", "") or ""),
                    "args": request.get("args", {}),
                    "id": str(request.get("id", "") or ""),
                }
            ],
        ),
    ]


def append_final_ai_response(
    messages: list[BaseMessage],
    final_answer: str,
) -> list[BaseMessage]:
    """Append a final assistant response to the durable history."""

    return [*messages, AIMessage(content=final_answer)]


def append_tool_message(
    messages: list[BaseMessage],
    request: ToolRequest,
    content: str,
) -> list[BaseMessage]:
    """Append a compact ToolMessage for a prior assistant tool call."""

    tool_call_id = str(request.get("id", "") or "")
    if not tool_call_id:
        return messages

    return [
        *messages,
        ToolMessage(
            content=content,
            name=str(request.get("name", "") or ""),
            tool_call_id=tool_call_id,
        ),
    ]


def _safe_compact_value(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    suffix = "... [truncated]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def _snapshot_tool_message(
    result: ToolResult,
    compact: CompactToolObservation,
    refs: list[str],
) -> str:
    tool_name = str(result.get("name", "browser_snapshot") or "browser_snapshot")
    status = str(result.get("status", "error") or "error")
    if status == "error":
        error = _safe_compact_value(result.get("error", "") or "Snapshot failed.", 400)
        return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

    summary = _safe_compact_value(compact.get("summary"), 300)
    if not summary or summary.startswith("browser_snapshot returned success:"):
        summary = f"Snapshot captured with {len(refs)} refs."

    parts = [tool_name, summary]
    if refs:
        parts.extend(["Refs:", "\n".join(refs[:MAX_TOOL_MESSAGE_REFS])])
    return "\n\n".join(part for part in parts if part)


def tool_result_message_content(
    result: ToolResult,
    compact: CompactToolObservation,
    refs: list[str],
    observation: str,
) -> str:
    """Build a compact ToolMessage body without embedding raw browser artifacts."""

    tool_name = str(result.get("name", "tool") or "tool").strip()
    if tool_name == "browser_snapshot":
        return _snapshot_tool_message(result, compact, refs)

    status = str(result.get("status", "error") or "error")
    if status == "error":
        error = _safe_compact_value(result.get("error", "") or observation, 500)
        return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

    summary = _safe_compact_value(compact.get("summary") or observation, 500)
    return "\n\n".join(part for part in [tool_name, summary] if part)
