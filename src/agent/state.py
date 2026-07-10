"""Shared state and data contracts for the AutoBrowser agent graph."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from langchain_core.messages import BaseMessage


AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]

MAX_REPLANS = 3
MAX_CONSECUTIVE_FAILURES = 3
MAX_SNAPSHOT_RECOVERIES = 1


class PlanStep(TypedDict, total=False):
    """Single planner step."""

    id: int
    description: str
    status: Literal["pending", "in_progress", "done"]


class ToolRequest(TypedDict, total=False):
    """Normalized tool call selected by the reasoning agent."""

    name: str
    args: dict[str, Any]
    reason: str
    id: str


class ToolResult(TypedDict, total=False):
    """Normalized result returned by the executor."""

    name: str
    status: ToolStatus
    content: str
    error: str


class CompactToolObservation(TypedDict, total=False):
    """Stateless LLM compression of a single tool result."""

    summary: str
    visible_state: str
    important_refs: list[str]
    errors: list[str]
    next_observation_hint: str


class AgentState(TypedDict, total=False):
    """Top-level graph state for the Plan -> Execute -> Observe loop."""

    task: str
    plan: list[PlanStep]
    current_step: int

    decision: AgentDecision
    tool_request: ToolRequest
    tool_result: ToolResult
    policy_decision: PolicyDecision

    observation: str
    snapshot: str
    refs: list[str]
    messages: list[BaseMessage]

    last_tool: str
    last_args: dict[str, Any]
    repeat_count: int
    replan_count: int
    consecutive_failures: int
    snapshot_recovery_count: int
    invalid_ref_recovery_count: int
    stale_snapshot_retries: int
    needs_fresh_snapshot: bool

    final_answer: str
    error: str
    human_approval: NotRequired[Any]
