"""Provider-neutral state contracts and control-loop thresholds.

Single, dependency-free home for the typed tool/plan/observation contracts and the
control-loop thresholds shared across the agent-loop layers.

This module imports nothing from ``src/agent_loop/``, ``src/harness/`` or
``src/browser/``. That is the whole point: any layer can depend on it without a
circular import. Keep it that way — only standard-library / ``typing`` imports
belong here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ToolDef:
    """Model-visible tool schema, independent of any provider.

    ``input_schema`` is a JSON Schema object (the MCP shape). Each chat provider
    gets a thin adapter that normalizes it into its own wire format (for example
    the OpenAI ``{"type": "function", ...}`` envelope Ollama expects). The
    executable handler is not part of this schema — :class:`Tool` pairs a
    ``ToolDef`` shape with an async invoker at registration time.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    """Provider-neutral executable tool: a ``ToolDef`` schema plus an async invoker.

    The harness registry and executor hold these; the model sees only the schema
    (via :meth:`Tool.to_def`) while the executor dispatches to :meth:`Tool.invoke`.
    ``input_schema`` is a JSON-Schema object, so any chat provider can advertise
    the tool without a framework-specific wrapper.

    ``func`` is an async callable invoked as ``func(**args)``.
    """

    name: str
    func: Callable[..., Awaitable[Any]] = field(repr=False, compare=False)
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_def(self) -> ToolDef:
        """Return the model-visible ``ToolDef`` schema for this tool."""

        return ToolDef(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    async def invoke(self, args: Mapping[str, Any] | None = None) -> Any:
        """Execute the tool with ``args`` as keyword arguments."""

        return await self.func(**dict(args or {}))


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
    "Tool",
    "ToolDef",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
]
