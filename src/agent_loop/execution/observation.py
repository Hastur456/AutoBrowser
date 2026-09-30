"""Engine-native observation compilation, split into explicit responsibilities.

- :class:`ToolResultNormalizer` — classify the latest raw ``ToolResult`` into neutral facts
  (name, status, whether it is a browser tool / a snapshot) with no state mutation.
- :class:`BrowserStateReducer` — keep the latest ``snapshot``; drop it after any other
  successful browser tool (the page may have changed) unless the server annotated that tool
  ``readOnlyHint``.
- :class:`ProgressDetector` — success/failure accounting: ``consecutive_failures`` and
  ``error``.
- :class:`ObservationCompiler` — orchestrate the above into the flat update dict, append the
  call to the task's action journal (:mod:`~src.agent_loop.execution.progress`), and build the
  model-facing observation text + tool message (with a repeat note when the call reproduced an
  earlier identical outcome). No completion policy lives inside observation building.

Server-neutral: tool output is rendered as text without parsing any server-specific schema
(no element refs, no error-text or error-code heuristics, no tab-list parsing, no action
classification) and tool names are compared exactly as exposed. How to react to a failed or
ineffective action is left to the model, which sees the tool output.

Plan progression is model/loop-driven on the engine-native path: no keyword-heuristic plan
advancement from observation text. The tool-message builders in ``harness/memory.py`` are
reused directly. This module imports nothing from ``src/agent/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config import get_settings
from src.contracts import CompactToolObservation, ToolResult
from src.harness.memory import append_tool_message, tool_result_message_content

from src.agent_loop.execution.policy import SNAPSHOT_TOOL, TABS_TOOL, is_browser_tool
from src.agent_loop.execution.progress import record_action, repeat_note
from src.agent_loop.execution.state import LoopState


# --------------------------------------------------------------------------- text helpers


def _raw_text(value: Any) -> str:
    """Normalized text without truncation."""

    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _compact_text(value: Any, limit: int | None = None) -> str:
    """Deterministic text preview within a character budget.

    ``limit`` defaults to ``settings.observation.max_content_preview_chars`` (read at call
    time so configuration changes take effect immediately).
    """

    if limit is None:
        limit = get_settings().observation.max_content_preview_chars
    text = _raw_text(value)
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    suffix = f"... [truncated {omitted} chars]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def _fallback_compact_observation(result: ToolResult) -> CompactToolObservation:
    """Deterministic compact observation when no observer LLM output is available."""

    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = str(result.get("status", "error") or "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error
    return {
        "summary": _compact_text(f"{tool_name} returned {status}: {payload}", 400),
        "visible_state": _compact_text(payload or "No tool output."),
        "errors": [error] if error else [],
        "next_observation_hint": "",
    }


def _observation_lines(
    result: ToolResult,
    compact: CompactToolObservation,
    *,
    compress: bool,
) -> list[str]:
    """Render a tool result into observation lines (no server-specific parsing)."""

    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error

    if status == "error":
        lines = ["Tool failed."]
        text = _compact_text(error or payload) if compress else _raw_text(error or payload)
        if text:
            lines.append(text)
        return lines

    if not compress:
        lines = [f"{tool_name} returned success."]
        if payload:
            lines.append(_raw_text(payload))
        return [line for line in lines if line]

    summary = compact.get("summary") or f"{tool_name} completed."
    visible_state = compact.get("visible_state") or payload
    lines = [_compact_text(summary, 400)]
    if visible_state:
        lines.append(_compact_text(visible_state))
    hint = compact.get("next_observation_hint", "")
    if hint:
        lines.append(_compact_text(hint, 300))
    return [line for line in lines if line]


# --------------------------------------------------------------------------- components


@dataclass(frozen=True)
class NormalizedToolResult:
    """Typed facts extracted from the latest raw ``ToolResult`` (no state mutation)."""

    result: dict[str, Any]
    request: dict[str, Any]
    tool_name: str
    status: Any
    content: str
    compact: CompactToolObservation
    is_browser_tool: bool
    is_snapshot: bool


@dataclass(frozen=True)
class BrowserReduction:
    """Browser-state updates (routed into ``BrowserState`` by ``LoopState.apply``)."""

    updates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressOutcome:
    """Success/failure accounting updates."""

    updates: dict[str, Any] = field(default_factory=dict)


class ToolResultNormalizer:
    """Classify the latest ``ToolResult`` into the neutral facts the reducers consume."""

    def normalize(
        self,
        state: LoopState,
        compact_observation: CompactToolObservation | None = None,
    ) -> NormalizedToolResult:
        result = state.tool_result or {}
        request = state.tool_request or {}
        tool_name = str(result.get("name", "") or "")
        status = result.get("status", "error")
        return NormalizedToolResult(
            result=result,
            request=request,
            tool_name=tool_name,
            status=status,
            content=str(result.get("content", "") or ""),
            compact=compact_observation or _fallback_compact_observation(result),
            is_browser_tool=is_browser_tool(tool_name),
            is_snapshot=tool_name == SNAPSHOT_TOOL and status == "success",
        )


class BrowserStateReducer:
    """Keep the latest snapshot.

    ``read_only_tools`` are the exposed names of tools whose server declared the MCP
    ``readOnlyHint`` annotation; they cannot change the page, so they keep the current
    snapshot valid. Every other successful browser tool invalidates it.
    """

    def __init__(self, read_only_tools: frozenset[str] = frozenset()) -> None:
        self._read_only_tools = frozenset(read_only_tools)

    def reduce(self, state: LoopState, norm: NormalizedToolResult) -> BrowserReduction:
        updates: dict[str, Any] = {}

        if norm.is_snapshot:
            updates["snapshot"] = norm.content
        elif (
            norm.is_browser_tool
            and norm.status == "success"
            and norm.tool_name not in self._read_only_tools
        ):
            if norm.tool_name == TABS_TOOL:
                updates["pending_browser_tab_index"] = 0
                updates["pending_browser_tab_reason"] = ""
            updates["snapshot"] = ""

        return BrowserReduction(updates=updates)


class ProgressDetector:
    """Account tool success/failure."""

    def detect(self, state: LoopState, norm: NormalizedToolResult) -> ProgressOutcome:
        if norm.status == "success":
            return ProgressOutcome(updates={"error": "", "consecutive_failures": 0})
        return ProgressOutcome(
            updates={
                "error": str(norm.result.get("error", "") or ""),
                "consecutive_failures": int(state.consecutive_failures or 0) + 1,
            }
        )


class ObservationCompiler:
    """Compose normalize -> reduce -> detect into the observation update dict.

    Builds the model-facing observation text and the tool message.
    """

    def __init__(
        self,
        *,
        normalizer: ToolResultNormalizer | None = None,
        reducer: BrowserStateReducer | None = None,
        detector: ProgressDetector | None = None,
        read_only_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._normalizer = normalizer or ToolResultNormalizer()
        self._reducer = reducer or BrowserStateReducer(read_only_tools)
        self._detector = detector or ProgressDetector()

    def compile(
        self,
        state: LoopState,
        compact_observation: CompactToolObservation | None = None,
        *,
        compress_tool_output: bool = False,
    ) -> dict[str, Any]:
        """Translate the latest ``ToolResult`` into a plain observation update dict.

        Does not touch ``plan``/``current_step``/``steps_without_plan_advance`` — plan
        progression is model/loop-driven on the engine-native path.
        """

        norm = self._normalizer.normalize(state, compact_observation)

        updates: dict[str, Any] = {
            "decision": "tool_call",
            "policy_decision": "",
            "tool_request": {},
        }
        updates.update(self._reducer.reduce(state, norm).updates)
        updates.update(self._detector.detect(state, norm).updates)

        record = record_action(
            state.action_history,
            norm.request,
            norm.result,
            preview_chars=get_settings().observation.action_history_preview_chars,
        )
        updates["action_history"] = [*state.action_history, record]
        note = repeat_note(record)

        lines = _observation_lines(norm.result, norm.compact, compress=compress_tool_output)
        observation = "\n\n".join([*lines, note] if note else lines)
        updates["observation"] = observation
        tool_message = tool_result_message_content(
            norm.result,
            norm.compact,
            observation,
            compress=compress_tool_output,
        )
        if note:
            tool_message = f"{tool_message}\n\n{note}"
        updates["messages"] = append_tool_message(list(state.messages or []), norm.request, tool_message)
        return updates


def compile_observation(
    state: LoopState,
    compact_observation: CompactToolObservation | None = None,
    *,
    compress_tool_output: bool = False,
) -> dict[str, Any]:
    """Backward-compatible thin wrapper over :class:`ObservationCompiler`."""

    return ObservationCompiler().compile(
        state,
        compact_observation,
        compress_tool_output=compress_tool_output,
    )


__all__ = [
    "BrowserReduction",
    "BrowserStateReducer",
    "NormalizedToolResult",
    "ObservationCompiler",
    "ProgressDetector",
    "ProgressOutcome",
    "ToolResultNormalizer",
    "compile_observation",
]
