"""Provider-neutral type-only state shapes.

``AgentState`` and its ``BrowserState`` sub-shape are type-only TypedDicts kept
for the harness/browser layers that still annotate their collaborators with the
full loop-state shape; nothing constructs them for control flow. The
provider-neutral tool/plan/observation contracts live in :mod:`src.contracts`.

This module imports nothing from ``src/agent_loop/``,
``src/harness/`` or ``src/browser/``.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from src.contracts import (
    AgentDecision,
    PlanStep,
    PolicyDecision,
    PolicyEvent,
    RecoveryCounters,
    ToolRequest,
    ToolResult,
)
from src.messages import Message


class BrowserState(TypedDict, total=False):
    """Browser context owned by observation state."""

    snapshot: str
    needs_fresh_snapshot: bool


class AgentState(TypedDict, total=False):
    """Top-level loop state for the Plan -> Execute -> Observe loop."""

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
    messages: list[Message]

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
    "AgentState",
    "BrowserState",
]
