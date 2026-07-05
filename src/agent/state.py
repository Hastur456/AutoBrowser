"""Shared state and data contracts for the AutoBrowser agent graph."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]
ObservationOutcome = Literal[
    "success",
    "no_output",
    "invalid_request",
    "unknown_tool",
    "transient_error",
    "blocked_error",
]
RecoveryAction = Literal["none", "retry", "replan", "ask_human", "stop"]


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


class ToolResult(TypedDict, total=False):
    """Normalized result returned by the executor."""

    name: str
    status: ToolStatus
    content: str
    error: str


class ExecutionEvent(TypedDict, total=False):
    """Compact append-only record of a tool execution."""

    sequence: int
    tool_name: str
    status: ToolStatus
    outcome: ObservationOutcome
    summary: str


class StructuredObservation(TypedDict, total=False):
    """Deterministic observation derived from the latest tool result."""

    tool_name: str
    status: ToolStatus
    outcome: ObservationOutcome
    summary: str
    content_preview: str
    error: str


class BrowserContext(TypedDict, total=False):
    """Compact browser facts for the next reasoning turn."""

    last_tool: str
    last_status: ToolStatus
    page_summary: str


class RecoverySignal(TypedDict, total=False):
    """Structured failure signal for later retry and recovery routing."""

    category: ObservationOutcome
    action: RecoveryAction
    reason: str
    repeat_count: int


class AgentState(TypedDict, total=False):
    """Top-level graph state.

    Nodes should return partial updates only. Lists are explicitly copied and
    appended by the node that owns the update to keep merge behavior obvious.
    """

    task: str
    plan: list[PlanStep]
    current_step: int
    decision: AgentDecision
    tool_request: ToolRequest
    tool_result: ToolResult
    policy_decision: PolicyDecision
    observation: str
    latest_observation: StructuredObservation
    browser_context: BrowserContext
    reasoning_context: str
    recovery_signal: RecoverySignal
    execution_events: list[ExecutionEvent]
    final_answer: str
    error: str
    history: list[str]
    human_approval: NotRequired[Any]
