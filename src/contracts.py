"""Provider-neutral state contracts.

Single, dependency-free home for the typed tool/plan/observation contracts shared
across the agent-loop layers.

Control-loop thresholds and tunables live in :mod:`src.config` (``settings.loop``),
which is equally dependency-free, so both the leaf contracts and the configured
thresholds stay reachable without a circular import. Keep this module free of
``src/agent_loop/``, ``src/harness/`` and ``src/browser/`` imports — that is the
whole point: any layer can depend on it. Only standard-library / ``typing``
imports belong here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]
GoalStatus = Literal["completed", "failed", "cancelled", "blocked"]
CompletionStatus = Literal["continue", "done", "blocked", "cancelled"]
HookEventName = Literal[
    "goal_start",
    "pre_tool_use",
    "permission_request",
    "post_tool_use",
    "post_tool_use_failure",
    "stop",
    "goal_end",
]
HookDecision = Literal["allow", "deny", "ask"]


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
    steps_without_plan_advance: int


class PolicyEvent(TypedDict, total=False):
    """Auditable policy decision context."""

    decision: PolicyDecision
    reason: str
    tool_request: ToolRequest
    human_response: Any


@dataclass(frozen=True)
class HookEvent:
    """One lifecycle point handed to hook handlers.

    Holds only dicts, tuples and scalars so ``json.dumps(dataclasses.asdict(event))``
    works — an out-of-process hook would receive the same object. ``args``/``result`` are
    copies, never references into loop state. Fields that do not apply to ``name`` keep
    their empty defaults.
    """

    name: HookEventName
    session_id: str | None
    goal_id: str
    task_id: str
    task: str
    #: Exposed tool name after request normalization.
    tool: str = ""
    #: ``MCPTool.server``; ``""`` for tools that are not MCP-backed.
    server: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: The ``ToolResult`` (``post_tool_use*`` only).
    result: dict[str, Any] = field(default_factory=dict)
    #: Reason of the built-in ``needs_human`` decision (``permission_request`` only).
    reason: str = ""
    #: The model's final answer (``stop`` only).
    final_answer: str = ""
    #: Evidence texts the final answer can be checked against (``stop`` only).
    evidence: tuple[str, ...] = ()
    #: ``True`` once a stop hook already rejected a completion in this task.
    stop_hook_active: bool = False
    #: Terminal status (``goal_end`` only).
    status: str = ""


@dataclass(frozen=True)
class HookResult:
    """What one handler says about a :class:`HookEvent`; ``None`` fields mean "no change"."""

    decision: HookDecision | None = None
    #: Shown to the model (a deny/ask reason or a completion rejection).
    reason: str = ""
    #: Replacement tool arguments (``pre_tool_use``).
    updated_input: dict[str, Any] | None = None
    #: Replacement tool output (``post_tool_use*``).
    updated_output: str | None = None
    #: Extra context delivered to the model as a separate message.
    additional_context: str = ""
    #: Shown only in events / the CLI, never to the model.
    user_message: str = ""


HookHandler = Callable[[HookEvent], Awaitable[HookResult | None]]


def goal_status_from_completion(status: CompletionStatus) -> GoalStatus | None:
    """Map a loop completion status into a terminal goal status.

    ``"done"`` becomes ``"completed"``; ``"blocked"`` and ``"cancelled"`` map through;
    any other value (such as the non-terminal ``"continue"``) is not a terminal goal
    outcome and maps to ``None``.
    """

    if status == "done":
        return "completed"
    if status in {"blocked", "cancelled"}:
        return status
    return None


__all__ = [
    "AgentDecision",
    "CompactToolObservation",
    "CompletionStatus",
    "GoalStatus",
    "goal_status_from_completion",
    "HookDecision",
    "HookEvent",
    "HookEventName",
    "HookHandler",
    "HookResult",
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
