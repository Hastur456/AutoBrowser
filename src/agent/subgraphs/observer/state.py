"""Observer state contracts."""

from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import BaseMessage

from src.agent.state import CompactToolObservation, PlanStep, ToolRequest, ToolResult


class ObserverState(TypedDict, total=False):
    """State accepted and returned by the observer subgraph."""

    tool_request: ToolRequest
    tool_result: ToolResult
    compact_observation: CompactToolObservation
    observation: str
    decision: str
    policy_decision: str
    snapshot: str
    needs_fresh_snapshot: bool
    error: str
    messages: list[BaseMessage]
    plan: list[PlanStep]
    current_step: int
    consecutive_failures: int
    stale_snapshot_retries: int
    invalid_ref_recovery_count: int
