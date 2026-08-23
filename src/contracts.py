"""Provider-neutral state contracts and control-loop thresholds.

Single, dependency-free home for the typed tool/plan/observation contracts and the
control-loop thresholds shared by both agent-loop implementations:

- the legacy LangGraph graph in ``src/agent/`` — ``src/agent/state.py`` re-exports every
  name here unchanged for backward compatibility; and
- the engine-native execution package in ``src/agent_loop/execution/``.

This module imports nothing from ``src/agent/``, ``src/agent_loop/``, ``src/harness/`` or
``src/browser/``. That is the whole point: either agent loop can depend on it without a
circular import, and the engine-native path carries **no** dependency on the legacy graph.
Keep it that way — only standard-library / ``typing`` imports belong here.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]

# Control-loop thresholds. Shared here so the legacy graph and the engine-native loop
# cannot drift. ``MAX_SNAPSHOT_RECOVERIES`` is currently dormant (not exercised by the
# engine-native path).
MAX_REPLANS = 3
MAX_CONSECUTIVE_FAILURES = 3
MAX_SNAPSHOT_RECOVERIES = 1
MAX_STEPS_WITHOUT_PLAN_ADVANCE = 8
MAX_UNCHANGED_SNAPSHOTS = 3


class PlanStep(TypedDict, total=False):
    """Single planner step."""

    id: int
    description: str
    status: Literal["pending", "in_progress", "completed"]


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
    error_code: str


class CompactToolObservation(TypedDict, total=False):
    """Stateless LLM compression of a single tool result."""

    summary: str
    visible_state: str
    important_refs: list[str]
    errors: list[str]
    next_observation_hint: str


class RecoveryCounters(TypedDict, total=False):
    """Retry and recovery counters that protect the agent loop."""

    replan_count: int
    consecutive_failures: int
    repeat_count: int
    snapshot_recovery_count: int
    invalid_ref_recovery_count: int
    stale_snapshot_retries: int
    steps_without_plan_advance: int


class PolicyEvent(TypedDict, total=False):
    """Auditable policy decision context."""

    decision: PolicyDecision
    reason: str
    tool_request: ToolRequest
    human_response: Any


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPLANS",
    "MAX_SNAPSHOT_RECOVERIES",
    "MAX_STEPS_WITHOUT_PLAN_ADVANCE",
    "MAX_UNCHANGED_SNAPSHOTS",
    "AgentDecision",
    "CompactToolObservation",
    "PlanStep",
    "PolicyDecision",
    "PolicyEvent",
    "RecoveryCounters",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
]
