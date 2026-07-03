"""Shared state and data contracts for the AutoBrowser agent graph."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]


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
    final_answer: str
    error: str
    history: list[str]
    human_approval: NotRequired[Any]
