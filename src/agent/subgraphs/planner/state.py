"""Planner state contracts."""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import BaseMessage

from src.agent.state import PolicyDecision, ToolRequest
from src.agent.state import PlanStep


class PlannerState(TypedDict, total=False):
    """State accepted and returned by the planner subgraph."""

    task: str
    task_id: str
    observation: str
    plan: list[PlanStep]
    current_step: int
    decision: str
    replan_count: int
    error: str
    stale_snapshot_retries: int
    invalid_ref_recovery_count: int
    needs_fresh_snapshot: bool
    messages: list[BaseMessage]
    policy_decision: PolicyDecision
    tool_request: ToolRequest
    snapshot: str
    consecutive_failures: int
    metadata: dict[str, Any]
