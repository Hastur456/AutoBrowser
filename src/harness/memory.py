"""Memory and conversation history helpers for the AutoBrowser harness."""

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

from src.browser import is_browser_snapshot_name
from src.contracts import CompactToolObservation, ToolRequest, ToolResult
from src.state import AgentState
from src.harness.context import ContextBuilder

MAX_TOOL_MESSAGE_REFS = 25
ORIGINAL_USER_REQUEST_PREFIX = "Original user request:\n"
USER_REQUEST_PREFIX = "User request"


def ensure_message_history(
    state: AgentState,
    *,
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    """Return existing history and ensure the current user task is represented."""

    messages = list(state.get("messages") or [])
    task = str(state.get("task", "") or "Complete the task.").strip()
    task_id = str(state.get("task_id", "") or "").strip()
    if not any(message.type == "system" for message in messages):
        prompt = (
            system_prompt
            if system_prompt is not None
            else ContextBuilder().get_system_prompt()
        )
        messages.insert(0, SystemMessage(content=prompt))

    if task_id:
        request_content = f"{USER_REQUEST_PREFIX} ({task_id}):\n{task}"
        if not _has_human_message(messages, request_content):
            messages.append(HumanMessage(content=request_content))
    elif not any(
        message.type == "human"
        and str(message.content).startswith(ORIGINAL_USER_REQUEST_PREFIX)
        for message in messages
    ):
        insert_at = 1 if messages and messages[0].type == "system" else 0
        messages.insert(
            insert_at,
            HumanMessage(content=f"{ORIGINAL_USER_REQUEST_PREFIX}{task}"),
        )
    return _compact_snapshot_history(messages)


def _has_human_message(messages: list[BaseMessage], content: str) -> bool:
    return any(
        message.type == "human" and str(message.content) == content
        for message in messages
    )


def _compact_snapshot_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Replace every snapshot ``ToolMessage`` but the latest with a stale-ref marker.

    Snapshot accessibility trees are appended to the durable history in full (see
    :func:`append_tool_message`), so without compaction every page the agent ever
    visited re-enters the model context on each turn and quickly overflows the window.
    Only the most recent snapshot is usable anyway — refs are ephemeral and valid
    solely for the snapshot that produced them — so older snapshot bodies collapse to
    a short marker telling the model the refs are stale. The ``tool_call_id`` is
    preserved, keeping the ``AIMessage(tool_calls)`` → ``ToolMessage`` pairing valid
    for the chat API.
    """

    snapshot_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage)
        and is_browser_snapshot_name(str(message.name or ""))
        and str(message.content or "").strip()
    ]
    if len(snapshot_indices) <= 1:
        return messages

    for index in snapshot_indices[:-1]:
        previous = messages[index]
        # snapshot_indices are collected from ToolMessage entries only, so ``previous``
        # always carries a ``tool_call_id`` here.
        messages[index] = ToolMessage(
            content=(
                f"{previous.name or 'browser_snapshot'} (historical)\n"
                "Snapshot superseded by a more recent one. "
                "Use only the latest snapshot and its refs."
            ),
            name=previous.name,
            tool_call_id=previous.tool_call_id,
        )
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


def _raw_value(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


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


def _raw_tool_message(result: ToolResult, refs: list[str]) -> str:
    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = str(result.get("status", "error") or "error")
    content = _raw_value(result.get("content", ""))
    error = _raw_value(result.get("error", ""))

    parts = [tool_name, f"Tool returned {status}."]
    if content:
        parts.extend(["Content:", content])
    if error:
        parts.extend(["Error:", error])
    if refs:
        parts.extend(["Refs:", "\n".join(refs[:MAX_TOOL_MESSAGE_REFS])])
    return "\n\n".join(part for part in parts if part)


def tool_result_message_content(
    result: ToolResult,
    compact: CompactToolObservation,
    refs: list[str],
    observation: str,
    *,
    compress: bool = False,
) -> str:
    """Build a ToolMessage body, optionally compacting raw browser artifacts."""

    if not compress:
        return _raw_tool_message(result, refs)

    tool_name = str(result.get("name", "tool") or "tool").strip()
    if tool_name == "browser_snapshot":
        return _snapshot_tool_message(result, compact, refs)

    status = str(result.get("status", "error") or "error")
    if status == "error":
        error = _safe_compact_value(result.get("error", "") or observation, 500)
        return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

    summary = _safe_compact_value(compact.get("summary") or observation, 500)
    return "\n\n".join(part for part in [tool_name, summary] if part)


__all__ = [
    "append_ai_tool_call",
    "append_final_ai_response",
    "append_tool_message",
    "ensure_message_history",
    "tool_result_message_content",
    "with_tool_call_id",
]
