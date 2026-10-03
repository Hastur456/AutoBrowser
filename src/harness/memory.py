"""Functional history service: conversation history helpers for the AutoBrowser harness.

:class:`MemoryManager` is the provider-neutral owner of the message-shaping policy — it
seeds the durable history with the current user task, appends the assistant tool calls /
tool results / final answers the loop produces, compacts large tool outputs superseded by a
newer result of the same tool, clears the oldest tool outputs once the history outgrows
``memory.history_budget_chars``, folds finished tasks into one digest message each at the task
boundary (``memory.keep_recent_tasks``), and formats tool-message bodies. It is a **functional** service: every
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

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from src.config import MemorySettings, get_settings
from src.contracts import CompactToolObservation, ToolRequest, ToolResult
from src.messages import (
    Message,
    ToolCall,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)

if TYPE_CHECKING:  # importing src.agent_loop here would cycle back through its guards
    from src.agent_loop.context import ContextAssembler

ORIGINAL_USER_REQUEST_PREFIX = "Original user request:\n"
USER_REQUEST_PREFIX = "User request"
COMPACTED_TOOL_OUTPUT_PREFIX = "[compacted]"
CLEARED_TOOL_OUTPUT_PREFIX = "[cleared]"
TASK_DIGEST_PREFIX = "[harness] Previous task digest:"


class MemoryManager:
    """Functional history service over provider-neutral ``Message`` lists.

    ``settings`` pins the ``memory`` section (tests pass an explicit one); without it every
    call reads ``get_settings().memory``.
    """

    def __init__(
        self,
        context: ContextAssembler | None = None,
        *,
        settings: MemorySettings | None = None,
    ) -> None:
        self._context = context
        self._settings = settings

    def _memory_settings(self) -> MemorySettings:
        return self._settings if self._settings is not None else get_settings().memory

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
        return self.apply_history_budget(self.compact_snapshot_history(messages))

    def _system_prompt(self) -> str:
        if self._context is not None:
            return self._context.get_system_prompt()
        from src.agent_loop.context import ContextAssembler

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
        min_chars = self._memory_settings().compact_tool_output_min_chars
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

    def apply_history_budget(self, messages: Sequence[Message]) -> list[Message]:
        """Clear the oldest tool outputs until the history fits ``history_budget_chars``.

        Server-neutral, deterministic and idempotent (the analogue of the Claude API
        ``clear_tool_uses`` context edit):

        - a budget of ``0`` (the default) or a history within budget is returned unchanged;
        - non-``tool`` messages (system prompt, user requests, model calls), the newest
          ``keep_recent_tool_results`` tool outputs and outputs that are already compacted or
          cleared are never touched;
        - the remaining tool outputs are replaced oldest first by a ``[cleared]`` placeholder
          until the history fits. If it still does not fit, nothing else is removed.

        The ``tool_call_id`` is preserved, so ``assistant(tool_calls)`` → ``tool`` pairs stay
        valid; the Action History block keeps the outcome of every call independently.
        """

        history = list(messages)
        settings = self._memory_settings()
        budget = settings.history_budget_chars
        if budget <= 0:
            return history
        total = history_chars(history)
        if total <= budget:
            return history

        tool_indexes = [index for index, message in enumerate(history) if message.role == "tool"]
        keep = settings.keep_recent_tool_results
        protected = set(tool_indexes[-keep:]) if keep else set()
        for index in tool_indexes:
            if total <= budget:
                break
            message = history[index]
            content = str(message.content or "")
            if index in protected or content.startswith(
                (COMPACTED_TOOL_OUTPUT_PREFIX, CLEARED_TOOL_OUTPUT_PREFIX)
            ):
                continue
            name = str(message.name or "tool")
            placeholder = (
                f"{CLEARED_TOOL_OUTPUT_PREFIX} {name} output from an earlier step "
                f"({len(content)} chars) was removed to fit the context budget."
            )
            if len(placeholder) >= len(content):
                continue
            history[index] = tool_message(
                tool_call_id=str(message.tool_call_id or ""),
                content=placeholder,
                name=message.name,
            )
            total -= len(content) - len(placeholder)
        return history

    # -- task boundary --------------------------------------------------------

    def digest_tasks(self, messages: Sequence[Message]) -> list[Message]:
        """Fold every finished task older than the newest ``keep_recent_tasks`` into a digest.

        A task segment runs from its ``User request (<task_id>):`` message up to the next one;
        it is replaced, with all its tool calls and results, by **one** ``[harness] Previous
        task digest`` user message (the request, the final answer, a count of the tools it
        called). Messages before the first segment (the system prompt, earlier digests) stay
        in place, so the order is preserved and every ``tool_call_id`` pair stays whole.
        ``keep_recent_tasks == 0`` (the default) returns the history unchanged.

        Called once per task boundary by the session, never per turn.
        """

        history = list(messages)
        settings = self._memory_settings()
        keep = settings.keep_recent_tasks
        if keep <= 0:
            return history
        starts = [index for index, message in enumerate(history) if _is_task_request(message)]
        if len(starts) <= keep:
            return history

        folded = set(starts[: len(starts) - keep])
        result = history[: starts[0]]
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(history)
            segment = history[start:end]
            if start in folded:
                result.append(user_message(_task_digest(segment, settings)))
            else:
                result.extend(segment)
        return result

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

        settings = self._memory_settings()
        tool_name = str(result.get("name", "tool") or "tool").strip()
        if tool_name == "browser_snapshot":
            return _snapshot_tool_message(result, compact, settings)

        limit = settings.compressed_tool_result_chars
        status = str(result.get("status", "error") or "error")
        if status == "error":
            error = _safe_compact_value(result.get("error", "") or observation, limit)
            return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

        summary = _safe_compact_value(compact.get("summary") or observation, limit)
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


def history_chars(messages: Sequence[Message]) -> int:
    """Characters the history costs: message contents plus tool-call arguments as JSON."""

    total = 0
    for message in messages:
        total += len(str(message.content or ""))
        for call in message.tool_calls:
            total += len(call.name)
            total += len(json.dumps(call.arguments, ensure_ascii=False, default=str))
    return total


def _is_task_request(message: Message) -> bool:
    return getattr(message, "role", None) == "user" and str(message.content).startswith(
        f"{USER_REQUEST_PREFIX} ("
    )


def _task_digest(segment: Sequence[Message], settings: MemorySettings) -> str:
    _, _, request = str(segment[0].content).partition("\n")
    answer = ""
    tools: Counter[str] = Counter()
    for message in segment[1:]:
        if message.role != "assistant":
            continue
        if message.tool_calls:
            tools.update(call.name for call in message.tool_calls if call.name)
        elif str(message.content or "").strip():
            answer = str(message.content)
    tools_used = ", ".join(f"{name}×{count}" for name, count in tools.items()) or "none"
    answer = _safe_compact_value(answer, settings.digest_answer_chars) or "(no final answer)"
    return "\n".join(
        [
            TASK_DIGEST_PREFIX,
            f"- request: {_safe_compact_value(request, settings.digest_request_chars)}",
            f"- answer: {answer}",
            f"- tools used: {tools_used}",
        ]
    )


def _has_user_message(messages: Sequence[Message], content: str) -> bool:
    return any(
        message.role == "user" and str(message.content) == content
        for message in messages
    )


def _safe_compact_value(value: Any, limit: int) -> str:
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
    settings: MemorySettings,
) -> str:
    tool_name = str(result.get("name", "browser_snapshot") or "browser_snapshot")
    status = str(result.get("status", "error") or "error")
    if status == "error":
        error = _safe_compact_value(
            result.get("error", "") or "Snapshot failed.",
            settings.compressed_snapshot_error_chars,
        )
        return "\n\n".join(part for part in [tool_name, "Tool failed.", error] if part)

    summary = _safe_compact_value(
        compact.get("summary"), settings.compressed_snapshot_summary_chars
    )

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


class NullMemoryContext:
    """Persistent memory switched off: renders no ``Memory`` block.

    The default ``EngineResources.memory``; the real one is
    :class:`src.harness.memory_store.MemoryContext`.
    """

    def render(self, state: Mapping[str, Any]) -> str:
        _ = state
        return ""


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
    "CLEARED_TOOL_OUTPUT_PREFIX",
    "COMPACTED_TOOL_OUTPUT_PREFIX",
    "MemoryManager",
    "NullMemoryContext",
    "TASK_DIGEST_PREFIX",
    "USER_REQUEST_PREFIX",
    "append_ai_tool_call",
    "append_final_ai_response",
    "append_tool_message",
    "ensure_message_history",
    "history_chars",
    "tool_result_message_content",
    "with_tool_call_id",
]
