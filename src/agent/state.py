"""Shared state and data contracts for the AutoBrowser agent graph.

The provider-neutral contracts and control-loop thresholds now live in
:mod:`src.contracts` so both the legacy graph and the engine-native execution package
(``src/agent_loop/execution/``) can share them without either depending on the other.
They are re-exported here unchanged for backward compatibility — existing
``from src.agent.state import ...`` imports keep working.

``AgentState`` (the LangGraph graph state) and its ``BrowserState`` sub-shape are
graph-specific and remain defined in this module.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage

from src.contracts import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_SNAPSHOT_RECOVERIES,
    MAX_STEPS_WITHOUT_PLAN_ADVANCE,
    MAX_UNCHANGED_SNAPSHOTS,
    AgentDecision,
    CompactToolObservation,
    PlanStep,
    PolicyDecision,
    PolicyEvent,
    RecoveryCounters,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class BrowserState(TypedDict, total=False):
    """Browser context owned by observation state."""

    snapshot: str
    needs_fresh_snapshot: bool


class AgentState(TypedDict, total=False):
    """Top-level graph state for the Plan -> Execute -> Observe loop."""

    task: str
    task_id: str
    plan: list[PlanStep]
    current_step: int

    decision: AgentDecision
    tool_request: ToolRequest
    tool_result: ToolResult
    policy_decision: PolicyDecision

    observation: str
    snapshot: str
    browser: BrowserState
    messages: list[BaseMessage]

    last_tool: str
    last_args: dict[str, Any]
    last_tool_request: ToolRequest
    repeat_count: int
    replan_count: int
    consecutive_failures: int
    snapshot_recovery_count: int
    invalid_ref_recovery_count: int
    stale_snapshot_retries: int
    ineffective_action_count: int
    unchanged_snapshot_count: int
    needs_fresh_snapshot: bool
    counters: RecoveryCounters

    snapshot_before_last_browser_action: str
    last_browser_action: ToolRequest
    ineffective_browser_action: ToolRequest
    ineffective_browser_actions: list[ToolRequest]
    pending_browser_tab_index: int
    pending_browser_tab_reason: str

    final_answer: str
    error: str
    policy_event: NotRequired[PolicyEvent]


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPLANS",
    "MAX_SNAPSHOT_RECOVERIES",
    "MAX_STEPS_WITHOUT_PLAN_ADVANCE",
    "MAX_UNCHANGED_SNAPSHOTS",
    "AgentDecision",
    "AgentState",
    "BrowserState",
    "CompactToolObservation",
    "PlanStep",
    "PolicyDecision",
    "PolicyEvent",
    "RecoveryCounters",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
]
