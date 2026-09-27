"""Functional history service: conversation history helpers for the AutoBrowser harness.

:class:`MemoryManager` is the provider-neutral owner of the message-shaping policy — it
seeds the durable history with the current user task, appends the assistant tool calls /
tool results / final answers the loop produces, compacts large tool outputs superseded by a
newer result of the same tool, and formats tool-message bodies. It is a **functional** service: every
operation takes a ``list`` of :class:`~src.messages.Message` (or the minimal state mapping
``ensure_history`` reads) and returns a *new* list; nothing is stored on the instance and
no input list is mutated in place.

The durable history itself lives **out of band** — in :attr:`LoopState.messages` and the
cross-task ``SessionContext.state`` carry-forward — never on the memory service. That is the
boundary this class keeps: it owns *how* the history is shaped, not *where* it is stored.

The module also keeps the historical module-level functions
(``ensure_message_history``, ``append_ai_tool_call``, ``append_final_ai_response``,
``append_tool_message``, ``tool_result_message_content``, ``with_tool_call_id``) as thin
delegating aliases over a default :class:`MemoryManager`, so the engine leaves that import
them (guards, observation, policy, the loop) keep working unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from src.agent_loop.context import ContextAssembler
from src.config import get_settings
from src.contracts import CompactToolObservation, ToolRequest, ToolResult
from src.messages import (
    Message,
    ToolCall,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)

ORIGINAL_USER_REQUEST_PREFIX = "Original user request:\n"
USER_REQUEST_PREFIX = "User request"
COMPACTED_TOOL_OUTPUT_PREFIX = "[compacted]"


class MemoryManager:
    """Functional history service over provider-neutral ``Message`` lists."""

    def __init__(self, context: ContextAssembler | None = None) -> None:
        self._context = context

    # -- seeding ------------------------------------------------------------

    def ensure_history(
        self,
        state: Mapping[str, Any],
        *,
        system_prompt: str | None = None,
    ) -> list[Message]:
        """Return existing history and ensure the current user task is represented."""

        messages = list(state.get("messages") or [])
        task = str(state.get("task", "") or "Complete the task.").strip()
        task_id = str(state.get("task_id", "") or "").strip()
        if not any(message.role == "system" for message in messages):
            prompt = (
                system_prompt
                if system_prompt is not None
                else self._system_prompt()
            )
            messages.insert(0, system_message(prompt))

        if task_id:
            request_content = f"{USER_REQUEST_PREFIX} ({task_id}):\n{task}"
            if not _has_user_message(messages, request_content):
                messages.append(user_message(request_content))
        elif not any(
            message.role == "user"
            and str(message.content).startswith(ORIGINAL_USER_REQUEST_PREFIX)
            for message in messages
        ):
            insert_at = 1 if messages and messages[0].role == "system" else 0
            messages.insert(
                insert_at,
                user_message(f"{ORIGINAL_USER_REQUEST_PREFIX}{task}"),
            )
        return self.compact_snapshot_history(messages)

    def _system_prompt(self) -> str:
        if self._context is not None:
            return self._context.get_system_prompt()
        return ContextAssembler().get_system_prompt()

    # -- compaction ---------------------------------------------------------

    def compact_snapshot_history(self, messages: Sequence[Message]) -> list[Message]:
        """Compact large tool outputs that a newer result of the same tool supersedes.

        Tool outputs (snapshots above all) are appended to the durable history in full (see
        :meth:`append_tool_result`), so without compaction every page the agent ever visited
        re-enters the model context on each turn and quickly overflows the window.

        The rule is server-neutral — no tool names are special-cased:

        - an output is compacted only when a *later* ``tool`` message from the **same tool**
          exists (a newer snapshot supersedes an older snapshot, never an unrelated result);
        - outputs no longer than ``settings.memory.compact_tool_output_min_chars`` are never
          compacted: short results (errors, empty lists, short text) are cheap and are exactly
          the evidence the model needs to see which attempts already failed.

        The ``tool_call_id`` is preserved, keeping the ``assistant(tool_calls)`` → ``tool``
        pairing valid for the chat API.
        """

        history = list(messages)
        min_chars = get_settings().memory.compact_tool_output_min_chars
        newer_tools: set[str] = set()
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.role != "tool" or not message.name:
                continue
            name = str(message.name)
            content = str(message.content or "")
            superseded = name in newer_tools
            newer_tools.add(name)
            if (
                not superseded
                or len(content) <= min_chars
                or content.startswith(COMPACTED_TOOL_OUTPUT_PREFIX)
            ):
                continue
            history[index] = tool_message(
                tool_call_id=str(message.tool_call_id or ""),
                content=(
                    f"{COMPACTED_TOOL_OUTPUT_PREFIX} {name} output from an earlier step "
                    f"({len(content)} chars). A newer {name} result appears later in "
                    "this conversation."
                ),
                name=message.name,
            )
        return history

    # -- appends ------------------------------------------------------------

    def append_tool_call(
        self,
        messages: Sequence[Message],
        request: ToolRequest,
    ) -> list[Message]:
        """Append the assistant tool call selected by the reasoning LLM."""

        args = request.get("args")
        arguments = args if isinstance(args, dict) else {}
        return [
            *messages,
            assistant_message(
                tool_calls=(
                    ToolCall(
                        id=str(request.get("id", "") or ""),
                        name=str(request.get("name", "") or ""),
                        arguments=dict(arguments),
                    ),
                ),
            ),
        ]

    def append_final(
        self,
        messages: Sequence[Message],
        final_answer: str,
    ) -> list[Message]:
        """Append a final assistant response to the durable history."""

        return [*messages, assistant_message(content=str(final_answer))]

    def append_tool_result(
        self,
        messages: Sequence[Message],
        request: ToolRequest,
        content: str,
    ) -> list[Message]:
        """Append a compact tool message for a prior assistant tool call."""

        tool_call_id = str(request.get("id", "") or "")
        if not tool_call_id:
            return list(messages)

        return [
            *messages,
            tool_message(
                tool_call_id=tool_call_id,
                content=content,
                name=str(request.get("name", "") or "") or None,
            ),
        ]

    # -- tool-message content ------------------------------------------------

    def tool_result_content(
        self,
        result: ToolResult,
        compact: CompactToolObservation,
        observation: str,
        *,
        compress: bool = False,
    ) -> str:
        """Build a tool-message body, optionally compacting raw browser artifacts."""

        if not compress:
            return _raw_tool_message(result)

        tool_name = str(result.get("name", "tool") or "tool").strip()
        if tool_name == "browser_snapshot":
            return _snapshot_tool_message(result, compact)

        status = str(result.get("status", "error") or "error")
        if status == "error":
            error = _safe_compact_value(result.get("error", "") or observation, 500)
            return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

        summary = _safe_compact_value(compact.get("summary") or observation, 500)
        return "\n\n".join(part for part in [tool_name, summary] if part)

    @staticmethod
    def with_tool_call_id(request: ToolRequest) -> ToolRequest:
        """Return a copy of a tool request with a stable chat tool-call id."""

        updated: ToolRequest = {
            "name": str(request.get("name", "") or ""),
            "args": request.get("args") if isinstance(request.get("args"), dict) else {},
            "reason": str(request.get("reason", "") or ""),
            "id": str(request.get("id", "") or f"call_{uuid4().hex}"),
        }
        return updated


def _has_user_message(messages: Sequence[Message], content: str) -> bool:
    return any(
        message.role == "user" and str(message.content) == content
        for message in messages
    )


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
) -> str:
    tool_name = str(result.get("name", "browser_snapshot") or "browser_snapshot")
    status = str(result.get("status", "error") or "error")
    if status == "error":
        error = _safe_compact_value(result.get("error", "") or "Snapshot failed.", 400)
        return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

    summary = _safe_compact_value(compact.get("summary"), 300)

    parts = [tool_name, summary]

    return "\n\n".join(part for part in parts if part)


def _raw_tool_message(result: ToolResult) -> str:
    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = str(result.get("status", "error") or "error")
    content = _raw_value(result.get("content", ""))
    error = _raw_value(result.get("error", ""))

    parts = [tool_name, f"Tool returned {status}."]
    if content:
        parts.extend(["Content:", content])
    if error:
        parts.extend(["Error:", error])
    return "\n\n".join(part for part in parts if part)


# Thin module-level aliases over a default MemoryManager. The engine leaves that
# import the historical function names keep working unchanged; new code can use the class.
_DEFAULT_MEMORY = MemoryManager()


def ensure_message_history(
    state: Mapping[str, Any],
    *,
    system_prompt: str | None = None,
) -> list[Message]:
    """Delegate to :meth:`MemoryManager.ensure_history` (module-level compat)."""

    return _DEFAULT_MEMORY.ensure_history(state, system_prompt=system_prompt)


def with_tool_call_id(request: ToolRequest) -> ToolRequest:
    """Delegate to :meth:`MemoryManager.with_tool_call_id` (module-level compat)."""

    return MemoryManager.with_tool_call_id(request)


def append_ai_tool_call(
    messages: Sequence[Message],
    request: ToolRequest,
) -> list[Message]:
    """Delegate to :meth:`MemoryManager.append_tool_call` (module-level compat)."""

    return _DEFAULT_MEMORY.append_tool_call(messages, request)


def append_final_ai_response(
    messages: Sequence[Message],
    final_answer: str,
) -> list[Message]:
    """Delegate to :meth:`MemoryManager.append_final` (module-level compat)."""

    return _DEFAULT_MEMORY.append_final(messages, final_answer)


def append_tool_message(
    messages: Sequence[Message],
    request: ToolRequest,
    content: str,
) -> list[Message]:
    """Delegate to :meth:`MemoryManager.append_tool_result` (module-level compat)."""

    return _DEFAULT_MEMORY.append_tool_result(messages, request, content)


def tool_result_message_content(
    result: ToolResult,
    compact: CompactToolObservation,
    observation: str,
    *,
    compress: bool = False,
) -> str:
    """Delegate to :meth:`MemoryManager.tool_result_content` (module-level compat)."""

    return _DEFAULT_MEMORY.tool_result_content(
        result,
        compact,
        observation,
        compress=compress,
    )


__all__ = [
    "MemoryManager",
    "append_ai_tool_call",
    "append_final_ai_response",
    "append_tool_message",
    "ensure_message_history",
    "tool_result_message_content",
    "with_tool_call_id",
]
