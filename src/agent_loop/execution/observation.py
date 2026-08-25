"""Engine-native observation compilation, split into explicit responsibilities.

Ported from ``src/agent/subgraphs/observer/nodes.py`` (``compile_observation`` and its
snapshot-fingerprint / action-identity helpers) but restructured into named, single-purpose
components instead of one function that does everything:

- :class:`ToolResultNormalizer` — classify the latest raw ``ToolResult`` into neutral facts
  (status, tool kind, refs, pending-tab activation, fresh-snapshot-after-error signals) with
  no state mutation.
- :class:`BrowserStateReducer` — reduce the browser-facing state from those facts: set/clear
  ``snapshot``, ``needs_fresh_snapshot``, the unchanged-snapshot streak, ``last_browser_action``
  and its pre-action snapshot, and pending-tab tracking, plus ineffective-action detection for a
  snapshot that did not change the visible view.
- :class:`ProgressDetector` — failure/ineffective-action accounting: failed-browser-action
  detection, ``consecutive_failures``, ``error``, and stale/invalid-ref counter resets.
- :class:`ObservationCompiler` — orchestrate the above into the flat update dict and build the
  model-facing observation text + tool message. Terminal decisions are delegated to
  :class:`~src.agent_loop.execution.guards.CompletionController` (the unchanged-snapshot
  terminal), so no completion policy lives inside observation building.

Snapshot fingerprinting, unchanged-snapshot termination at :data:`MAX_UNCHANGED_SNAPSHOTS`,
ineffective-action detection, browser-state clearing, ``consecutive_failures`` accounting,
stale/invalid-ref handling, and pending-tab tracking are preserved byte-for-byte; only the
structure changes. State access is typed :class:`~src.agent_loop.execution.state.LoopState`
attribute access, and the returned flat update dict is applied through :meth:`LoopState.apply`.

Deliberately **not** ported: the legacy keyword-heuristic plan advancement
(``_plan_completion_update`` and its hardcoded natural-language term lists). The
engine-native path does not auto-advance the plan from observation text — plan progression
is driven by the planner/model (via ``update_plan``/replan) and the loop, not by matching
observation text against a hardcoded verb list.

The browser-schema observation leaves live in :mod:`src.browser.observation`; the tool-message
builders in ``harness/memory.py`` are reused directly. This module imports nothing from
``src/agent/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.contracts import CompactToolObservation
from src.browser.observation import (
    BROWSER_ACTION_TOOLS,
    _needs_fresh_snapshot_after_error,
    _observation_lines,
    extract_element_refs,
    fallback_compact_observation,
    pending_tab_activation_from_result,
    request_ref_value,
    snapshot_contains_ref,
)
from src.browser.adapters import element_description_from_snapshot
from src.harness.memory import append_tool_message, tool_result_message_content

from src.agent_loop.execution.guards import CompletionController
from src.agent_loop.execution.state import MAX_UNCHANGED_SNAPSHOTS, LoopState

_SNAPSHOT_UNCHANGED_NOTE = (
    "The last browser action did not change the visible snapshot. "
    "Do not repeat the same action with the same target; try a "
    "different visible control, a deeper snapshot, or a fallback route."
)
_BROWSER_ACTION_FAILED_NOTE = (
    "The browser action failed for this target. Do not repeat the "
    "same action with the same target after a recovery detour; choose "
    "a different control, use a direct fallback, or fail the goal."
)
_TIMED_OUT_STALE_REF_NOTE = (
    "The ref-based action timed out without a current matching ref in "
    "browser_snapshot; take a fresh browser_snapshot before the next "
    "ref-based action."
)


def _snapshot_fingerprint(snapshot: str) -> str:
    lines = []
    for line in snapshot.splitlines():
        normalized = re.sub(r"\s+\[(?:active|focused)\]", "", line.strip())
        normalized = re.sub(r"\[ref=[^\]]+\]", "[ref]", normalized)
        normalized = re.sub(r"\bref=[A-Za-z][A-Za-z0-9_-]*\b", "ref", normalized)
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _action_identity(
    request: dict[str, Any],
    tool_name: str,
    snapshot: str = "",
) -> dict[str, Any]:
    args = dict(request.get("args") or {})
    action: dict[str, Any] = {
        "name": tool_name,
        "args": args,
    }
    ref = str(args.get("ref") or args.get("target") or "")
    if ref and snapshot:
        action["target_description"] = element_description_from_snapshot(snapshot, ref)
    return action


def _same_action(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("name") == right.get("name") and left.get("args", {}) == right.get(
        "args", {}
    )


def _needs_fresh_snapshot_after_timeout(
    state: LoopState,
    result: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    tool_name = str(result.get("name", "") or "")
    payload = str(result.get("error", "") or result.get("content", "") or "").lower()
    if tool_name not in BROWSER_ACTION_TOOLS or "timeout" not in payload:
        return False

    requested_ref = request_ref_value(request)
    if not requested_ref:
        return False

    snapshot = str(state.browser.snapshot or "")
    return not snapshot.strip() or not snapshot_contains_ref(snapshot, requested_ref)


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
    is_browser_action: bool
    is_snapshot: bool
    timed_out_stale_ref: bool
    needs_fresh_after_error: bool
    refs: list[str]
    pending_tab_activation: tuple[int, str] | None


@dataclass(frozen=True)
class BrowserReduction:
    """Browser-state updates (routed into ``BrowserState`` by ``LoopState.apply``)."""

    updates: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class ProgressOutcome:
    """Failure/ineffective-action accounting updates plus its observation note."""

    updates: dict[str, Any] = field(default_factory=dict)
    note: str = ""


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
        content = str(result.get("content", "") or "")
        compact = compact_observation or fallback_compact_observation(result)

        is_browser_tool = tool_name.startswith("browser_")
        is_browser_action = tool_name in BROWSER_ACTION_TOOLS
        is_snapshot = tool_name == "browser_snapshot" and status == "success"
        timed_out_stale_ref = status == "error" and _needs_fresh_snapshot_after_timeout(
            state,
            result,
            request,
        )
        needs_fresh_after_error = status == "error" and (
            _needs_fresh_snapshot_after_error(result) or timed_out_stale_ref
        )
        refs = extract_element_refs(content) if is_snapshot else []
        pending_tab_activation = pending_tab_activation_from_result(result)
        return NormalizedToolResult(
            result=result,
            request=request,
            tool_name=tool_name,
            status=status,
            content=content,
            compact=compact,
            is_browser_tool=is_browser_tool,
            is_browser_action=is_browser_action,
            is_snapshot=is_snapshot,
            timed_out_stale_ref=timed_out_stale_ref,
            needs_fresh_after_error=needs_fresh_after_error,
            refs=refs,
            pending_tab_activation=pending_tab_activation,
        )


class BrowserStateReducer:
    """Reduce the browser-facing state from a normalized tool result."""

    def reduce(self, state: LoopState, norm: NormalizedToolResult) -> BrowserReduction:
        updates: dict[str, Any] = {}
        note = ""

        if norm.is_snapshot:
            prior_unchanged_snapshots = int(state.unchanged_snapshot_count or 0)
            previous_browser_snapshot = str(state.browser.snapshot or "")
            current_fingerprint = _snapshot_fingerprint(norm.content)
            previous_browser_fingerprint = _snapshot_fingerprint(previous_browser_snapshot)

            if previous_browser_fingerprint and current_fingerprint == previous_browser_fingerprint:
                unchanged_snapshot_count = prior_unchanged_snapshots + 1
            else:
                unchanged_snapshot_count = 1

            updates["snapshot"] = norm.content
            updates["needs_fresh_snapshot"] = False
            updates["unchanged_snapshot_count"] = unchanged_snapshot_count
            previous_snapshot = str(state.browser.snapshot_before_last_browser_action or "")
            last_action = state.browser.last_browser_action or {}
            if previous_snapshot and last_action:
                previous_fingerprint = _snapshot_fingerprint(previous_snapshot)
                if current_fingerprint == previous_fingerprint:
                    prior_ineffective = state.browser.ineffective_browser_action or {}
                    prior_count = int(state.ineffective_action_count or 0)
                    prior_history = list(state.browser.ineffective_browser_actions or [])
                    updates["ineffective_browser_action"] = last_action
                    updates["ineffective_browser_actions"] = [
                        *prior_history,
                        last_action,
                    ][-5:]
                    updates["ineffective_action_count"] = (
                        prior_count + 1 if _same_action(prior_ineffective, last_action) else 1
                    )
                    note = _SNAPSHOT_UNCHANGED_NOTE
                else:
                    updates["ineffective_browser_action"] = {}
                    updates["ineffective_browser_actions"] = []
                    updates["ineffective_action_count"] = 0
            updates["snapshot_before_last_browser_action"] = ""
        elif norm.is_browser_tool and (
            norm.status == "success" or norm.needs_fresh_after_error
        ):
            if norm.status == "success":
                if norm.tool_name == "browser_tabs":
                    updates["pending_browser_tab_index"] = 0
                    updates["pending_browser_tab_reason"] = ""
                if norm.is_browser_action:
                    action_snapshot = str(state.browser.snapshot or "")
                    updates["snapshot_before_last_browser_action"] = action_snapshot
                    updates["last_browser_action"] = _action_identity(
                        norm.request,
                        norm.tool_name,
                        action_snapshot,
                    )
                updates["snapshot"] = ""
                updates["unchanged_snapshot_count"] = 0
                if norm.pending_tab_activation:
                    pending_tab_index, pending_tab_reason = norm.pending_tab_activation
                    updates["pending_browser_tab_index"] = pending_tab_index
                    updates["pending_browser_tab_reason"] = pending_tab_reason
            if norm.needs_fresh_after_error:
                updates["snapshot"] = ""
                updates["needs_fresh_snapshot"] = True
                updates["unchanged_snapshot_count"] = 0

        return BrowserReduction(updates=updates, note=note)


class ProgressDetector:
    """Detect failed/ineffective browser actions and account tool success/failure."""

    def detect(self, state: LoopState, norm: NormalizedToolResult) -> ProgressOutcome:
        updates: dict[str, Any] = {}
        note = ""

        if (
            norm.status == "error"
            and norm.is_browser_action
            and not norm.needs_fresh_after_error
        ):
            failed_action = _action_identity(
                norm.request,
                norm.tool_name,
                str(state.browser.snapshot or ""),
            )
            prior_ineffective = state.browser.ineffective_browser_action or {}
            prior_count = int(state.ineffective_action_count or 0)
            prior_history = list(state.browser.ineffective_browser_actions or [])
            updates["ineffective_browser_action"] = failed_action
            updates["ineffective_browser_actions"] = [
                *prior_history,
                failed_action,
            ][-5:]
            updates["ineffective_action_count"] = (
                prior_count + 1 if _same_action(prior_ineffective, failed_action) else 1
            )
            note = _BROWSER_ACTION_FAILED_NOTE

        if norm.status == "success":
            updates["error"] = ""
            updates["consecutive_failures"] = 0
            if norm.tool_name != "browser_snapshot":
                updates["stale_snapshot_retries"] = 0
                updates["invalid_ref_recovery_count"] = 0
        else:
            updates["error"] = str(norm.result.get("error", "") or "")
            updates["consecutive_failures"] = int(state.consecutive_failures or 0) + 1

        return ProgressOutcome(updates=updates, note=note)


class ObservationCompiler:
    """Compose normalize -> reduce -> detect into the observation update dict.

    Builds the model-facing observation text and the tool message, then delegates the
    unchanged-snapshot terminal to :class:`CompletionController` so completion policy is not
    scattered through observation building.
    """

    def __init__(
        self,
        *,
        normalizer: ToolResultNormalizer | None = None,
        reducer: BrowserStateReducer | None = None,
        detector: ProgressDetector | None = None,
        completion: CompletionController | None = None,
    ) -> None:
        self._normalizer = normalizer or ToolResultNormalizer()
        self._reducer = reducer or BrowserStateReducer()
        self._detector = detector or ProgressDetector()
        self._completion = completion or CompletionController()

    def compile(
        self,
        state: LoopState,
        compact_observation: CompactToolObservation | None = None,
        *,
        compress_tool_output: bool = False,
    ) -> dict[str, Any]:
        """Translate the latest ``ToolResult`` into a plain observation update dict.

        Does not touch ``plan``/``current_step``/``steps_without_plan_advance`` — plan
        progression is model/loop-driven on the engine-native path (see module docstring).
        """

        norm = self._normalizer.normalize(state, compact_observation)

        observation_lines = _observation_lines(
            norm.result, norm.compact, norm.refs, compress=compress_tool_output
        )
        if norm.timed_out_stale_ref:
            observation_lines.append(_TIMED_OUT_STALE_REF_NOTE)
        if norm.pending_tab_activation:
            _, pending_tab_reason = norm.pending_tab_activation
            observation_lines.append(pending_tab_reason)

        updates: dict[str, Any] = {
            "decision": "tool_call",
            "policy_decision": "",
            "tool_request": {},
        }

        browser = self._reducer.reduce(state, norm)
        updates.update(browser.updates)
        if browser.note:
            observation_lines.append(browser.note)

        progress = self._detector.detect(state, norm)
        updates.update(progress.updates)
        if progress.note:
            observation_lines.append(progress.note)

        observation = "\n\n".join(observation_lines)
        updates["observation"] = observation
        tool_message = tool_result_message_content(
            norm.result,
            norm.compact,
            norm.refs,
            observation,
            compress=compress_tool_output,
        )
        updates["messages"] = append_tool_message(
            list(state.messages or []), norm.request, tool_message
        )

        terminal = self._completion.observation_terminal_update(
            is_snapshot=norm.is_snapshot,
            status=norm.status,
            unchanged_snapshot_count=int(updates.get("unchanged_snapshot_count", 0) or 0),
            observation=observation,
        )
        if terminal:
            updates.update(terminal)

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
    "MAX_UNCHANGED_SNAPSHOTS",
    "BrowserReduction",
    "BrowserStateReducer",
    "NormalizedToolResult",
    "ObservationCompiler",
    "ProgressDetector",
    "ProgressOutcome",
    "ToolResultNormalizer",
    "compile_observation",
]
