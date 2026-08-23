"""Typed engine-native loop state for the explicit AutoBrowser execution loop.

`LoopState` replaces the legacy `AgentState` TypedDict for the engine-native path.
It is a frozen dataclass: every state transition returns a *new* `LoopState` via
:meth:`LoopState.apply`, which accepts the same flat update dicts the ported control
functions produce (mirroring the legacy graph-node update shape) and routes
browser-scoped keys into the nested :class:`BrowserState`.

Thresholds and typed contracts are imported (never redefined) from the neutral
``src.contracts`` module so the legacy graph and the engine-native path cannot drift and
the engine-native path carries no dependency on ``src/agent/``.

The dormant ``snapshot_recovery_count`` (``MAX_SNAPSHOT_RECOVERIES``) is intentionally
excluded — it is not exercised by the scenarios this slice targets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any

from langchain_core.messages import BaseMessage

from src.contracts import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_STEPS_WITHOUT_PLAN_ADVANCE,
    MAX_UNCHANGED_SNAPSHOTS,
    PlanStep,
    PolicyDecision,
    ToolRequest,
    ToolResult,
)

# Flat update keys that live on the nested BrowserState rather than LoopState.
BROWSER_STATE_FIELDS = frozenset(
    {
        "snapshot",
        "needs_fresh_snapshot",
        "snapshot_before_last_browser_action",
        "last_browser_action",
        "ineffective_browser_action",
        "ineffective_browser_actions",
        "pending_browser_tab_index",
        "pending_browser_tab_reason",
    }
)


@dataclass(frozen=True)
class BrowserState:
    """Browser context owned by the execution loop.

    Mirrors the flat browser-scoped fields of the legacy ``AgentState`` so the
    ported observation/guard logic keeps identical semantics.
    """

    snapshot: str = ""
    needs_fresh_snapshot: bool = False
    snapshot_before_last_browser_action: str = ""
    last_browser_action: ToolRequest = field(default_factory=dict)
    ineffective_browser_action: ToolRequest = field(default_factory=dict)
    ineffective_browser_actions: list[ToolRequest] = field(default_factory=list)
    pending_browser_tab_index: int = 0
    pending_browser_tab_reason: str = ""


@dataclass(frozen=True)
class LoopState:
    """Typed engine-native state for the Plan -> Execute -> Observe loop.

    Field set is the superset of the legacy ``AgentState`` that the ported control
    logic reads or writes, minus the vestigial ``counters``/``snapshot_recovery_count``.
    Browser-scoped fields live under :attr:`browser`.
    """

    task: str = ""
    task_id: str = ""

    plan: list[PlanStep] = field(default_factory=list)
    current_step: int = 0

    messages: list[BaseMessage] = field(default_factory=list)

    decision: str = ""
    tool_request: ToolRequest = field(default_factory=dict)
    tool_result: ToolResult = field(default_factory=dict)
    policy_decision: PolicyDecision | str = ""
    policy_event: dict[str, Any] = field(default_factory=dict)

    observation: str = ""
    browser: BrowserState = field(default_factory=BrowserState)

    last_tool: str = ""
    last_args: dict[str, Any] = field(default_factory=dict)
    last_tool_request: ToolRequest = field(default_factory=dict)
    repeat_count: int = 0
    replan_count: int = 0
    consecutive_failures: int = 0
    invalid_ref_recovery_count: int = 0
    stale_snapshot_retries: int = 0
    ineffective_action_count: int = 0
    unchanged_snapshot_count: int = 0
    steps_without_plan_advance: int = 0

    final_answer: str = ""
    error: str = ""

    def apply(self, updates: Mapping[str, Any]) -> LoopState:
        """Return a new ``LoopState`` with ``updates`` merged in.

        ``updates`` uses the same flat keys the legacy graph nodes returned; keys in
        :data:`BROWSER_STATE_FIELDS` are routed into :attr:`browser`. Unknown keys
        raise so transcription mistakes surface immediately rather than silently
        no-op'ing.
        """

        if not updates:
            return self

        loop_updates: dict[str, Any] = {}
        browser_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if key in BROWSER_STATE_FIELDS:
                browser_updates[key] = value
            elif key in _LOOP_STATE_FIELDS:
                loop_updates[key] = value
            else:
                raise ValueError(f"Unknown LoopState update key: {key!r}")

        if browser_updates:
            loop_updates["browser"] = replace(self.browser, **browser_updates)
        return replace(self, **loop_updates)

    def snapshot_mapping(self) -> dict[str, Any]:
        """Minimal mapping fed to ``BrowserProvider.normalize_request``.

        Browser providers read state via ``.get(...)`` (only ``snapshot`` today), so a
        plain dict keeps them unchanged while the loop uses the typed dataclass.
        """

        return {
            "snapshot": self.browser.snapshot,
            "needs_fresh_snapshot": self.browser.needs_fresh_snapshot,
            "error": self.error,
            "last_tool": self.last_tool,
            "last_args": dict(self.last_args),
            "snapshot_before_last_browser_action": (
                self.browser.snapshot_before_last_browser_action
            ),
            "last_browser_action": dict(self.browser.last_browser_action),
            "ineffective_browser_action": dict(self.browser.ineffective_browser_action),
            "ineffective_browser_actions": [
                dict(action) for action in self.browser.ineffective_browser_actions
            ],
            "pending_browser_tab_index": self.browser.pending_browser_tab_index,
            "pending_browser_tab_reason": self.browser.pending_browser_tab_reason,
        }

    def to_session_state(self) -> dict[str, Any]:
        """Return the durable cross-task carry-forward (the SESSION_STATE_KEYS subset).

        Shapes match ``src/harness/session.py:SESSION_STATE_KEYS`` so the existing
        carry-forward (``context.state.replace(...)``) keeps working unchanged.
        """

        return {
            "messages": list(self.messages),
            "observation": self.observation,
            "snapshot": self.browser.snapshot,
            "needs_fresh_snapshot": self.browser.needs_fresh_snapshot,
            "browser": {
                "snapshot": self.browser.snapshot,
                "needs_fresh_snapshot": self.browser.needs_fresh_snapshot,
            },
            "last_tool": self.last_tool,
            "last_args": dict(self.last_args),
            "last_tool_request": dict(self.last_tool_request),
            "snapshot_before_last_browser_action": (
                self.browser.snapshot_before_last_browser_action
            ),
            "last_browser_action": dict(self.browser.last_browser_action),
            "ineffective_browser_action": dict(self.browser.ineffective_browser_action),
            "ineffective_browser_actions": [
                dict(action) for action in self.browser.ineffective_browser_actions
            ],
            "pending_browser_tab_index": self.browser.pending_browser_tab_index,
            "pending_browser_tab_reason": self.browser.pending_browser_tab_reason,
        }

    @classmethod
    def from_state_overrides(
        cls,
        overrides: Mapping[str, Any] | None = None,
    ) -> LoopState:
        """Build an initial ``LoopState`` from carried-forward session state.

        Accepts the flat SESSION_STATE_KEYS dict (plus ``task``/``task_id``). A nested
        ``browser`` mapping is unpacked into the flat browser keys when the flat keys
        are absent. Unrecognized keys (e.g. task-local resets that are not LoopState
        fields) are ignored, so the same dict the harness carries can be passed
        verbatim.
        """

        state = cls()
        if not overrides:
            return state

        data = dict(overrides)
        browser = data.pop("browser", None)
        if isinstance(browser, Mapping):
            for key in ("snapshot", "needs_fresh_snapshot"):
                if key not in data and key in browser:
                    data[key] = browser[key]

        known = {
            key: value
            for key, value in data.items()
            if key in BROWSER_STATE_FIELDS or key in _LOOP_STATE_FIELDS
        }
        return state.apply(known)


_LOOP_STATE_FIELDS = frozenset(f.name for f in fields(LoopState))


__all__ = [
    "BROWSER_STATE_FIELDS",
    "BrowserState",
    "LoopState",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPLANS",
    "MAX_STEPS_WITHOUT_PLAN_ADVANCE",
    "MAX_UNCHANGED_SNAPSHOTS",
]
