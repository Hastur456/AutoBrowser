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

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

AgentDecision = Literal["tool_call", "replan", "done"]
PolicyDecision = Literal["approved", "needs_human", "blocked"]
ToolStatus = Literal["success", "error"]
GoalStatus = Literal["completed", "failed", "cancelled", "blocked"]
CompletionStatus = Literal["continue", "done", "blocked", "cancelled"]
#: Machine-readable reason a task ended, beside the prose of its final answer.
StopReason = Literal[
    "answered",
    "model_stop",
    "turn_cap",
    "max_replans",
    "consecutive_failures",
    "no_plan_advance",
    "approval_denied",
    "approval_hook_denied",
    "goal_hook_denied",
    "no_action",
    "invalid_decision",
    "cancelled",
    # The task's model-call, token or cost budget ran out before the next model call.
    "budget_exhausted",
    # The run needed a ladder stage above ``RunProfile.max_stage``.
    "ladder_cap",
    # A recording finished the task without the agent (an output region or a summary).
    "playback_completed",
]
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
PermissionDecision = Literal["allow", "ask", "deny"]
PermissionMode = Literal["default", "read_only", "dont_ask", "bypass"]
#: What decided a :class:`PermissionVerdict`: a config rule (``permissions.rules``), a
#: ``pre_tool_use`` hook ``ask``, the acting model's own ``approval_request``, the approval
#: classifier, the mode default, the ``readOnlyHint`` annotation, a session grant, or a failed
#: evaluation (always ``deny``).
PermissionSource = Literal[
    "rule", "hook", "model", "classifier", "mode", "annotation", "grant", "error", "task_scope"
]
#: Who besides the rules may escalate a call to human approval
#: (``settings.permissions.approval_judge``): nobody, the acting model, a separate classifier
#: model call, or both.
ApprovalJudgeMode = Literal["off", "model", "classifier", "both"]
#: A human's answer to an approval prompt: run this call, run it and remember the grant for
#: the session, or refuse.
ApprovalAnswer = Literal["once", "session", "deny"]
#: Who answered an approval: a front end, or nobody in time (``timeout`` is a deny).
#: Who answered an approval; ``timeout`` and ``cancelled`` (the task stopped while waiting)
#: are always a ``deny``.
ApprovalVia = Literal["tui", "plain", "classic", "web", "timeout", "cancelled", "api", "channel"]
#: What a persistent memory file describes: one site (``sites/<domain>.md``) or a
#: reusable procedure (``procedures/<name>.md``).
MemoryKind = Literal["site", "procedure"]
#: Trust of a memory entry: written by the human, confirmed by successful tasks, written by
#: the agent and not yet confirmed, or contradicted by failing tasks / too old.
MemoryStatus = Literal["user", "verified", "unverified", "stale"]
#: Stages of the cost ladder, cheapest first: an output probe without the model, playback of
#: a recording, a one-step model retarget, the agent, a human approval.
#: Names of recordings and stand jobs: lowercase letters, digits and single hyphens, 1-64
#: characters (the Agent Skills naming rule), so a name can never leave its directory.
RECORDING_NAME_PATTERN = r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$"
LadderStage = Literal["probe", "playback", "retarget", "agent", "human"]
#: The order of the cost ladder; a run never climbs above ``RunProfile.max_stage``.
LADDER_ORDER: tuple[LadderStage, ...] = ("probe", "playback", "retarget", "agent", "human")
#: How a finished playback answers the task: one agent turn, the region of the last tool
#: output, or a fixed summary of the replayed steps.
PlaybackAnswer = Literal["agent", "output", "summary"]
#: Why a step source hands the rest of the task to the model.
StepHandoffKind = Literal[
    "binding",  # a recorded reference has no unique match on the current page
    "tool_error",  # the replayed call failed
    "checkpoint",  # the output after the step does not look like the recorded one
    "catalog",  # the tool is gone, or its schema or readOnlyHint changed
    "denied",  # a guard, hook or permission rule blocked the replayed call
    "answer",  # every step ran; the agent answers the task
    "exhausted",  # the source cannot go on (a limit, a stage cap, a step it must not replay)
    "repeat",  # the engine's repeat guard stopped an identical replayed call
]


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

    ``func`` is an async callable invoked as ``func(**args)``. ``annotations`` are the
    MCP tool annotations (``readOnlyHint`` ...) when the tool declares any, the same
    protocol metadata an ``MCPTool`` carries.
    """

    name: str
    func: Callable[..., Awaitable[Any]] = field(repr=False, compare=False)
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] | None = None

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
    #: Reason of the permission ``ask`` verdict (``permission_request`` only).
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


@dataclass(frozen=True)
class PermissionCheck:
    """The call a ``PermissionEngine`` authorizes: the final normalized request.

    Holds only dicts and scalars, like :class:`HookEvent`. (Not ``PermissionRequest``: that
    name belongs to the ``permission_request`` hook event.)
    """

    #: Exposed tool name after request normalization (and any hook ``updated_input``).
    tool: str
    #: ``MCPTool.server``; ``""`` for tools that are not MCP-backed.
    server: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: The server declared the MCP ``readOnlyHint`` annotation.
    read_only: bool = False
    #: Reason of a ``pre_tool_use`` hook ``ask``; escalates the call to approval.
    hook_ask_reason: str = ""
    #: The acting model's own ``approval_request`` for this call; escalates it to approval.
    model_ask_reason: str = ""
    #: Reason of the approval classifier's "needs approval"; escalates the call to approval.
    classifier_ask_reason: str = ""
    #: The task's allowlist (``RunProfile.allowed_domains``, normalized); non-empty: a call on
    #: another domain, or a state-changing call on no known domain, is denied (task scope).
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionVerdict:
    """The authorization decision for one :class:`PermissionCheck`."""

    decision: PermissionDecision
    #: Shown to the model on a deny; never quotes the arguments.
    reason: str
    source: PermissionSource = "mode"
    #: Id of the deciding rule; ``""`` for mode, annotation, hook and error decisions.
    rule_id: str = ""
    #: No session grant can cover this call (``always_ask`` rule, or a hook, model or
    #: classifier ``ask``).
    always_ask: bool = False
    #: ``(server, tool, domain)`` a "for this session" approval would grant; ``None`` when
    #: the call cannot be granted.
    grant_key: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class ApprovalTicket:
    """One approval waiting for a human, as every front end shows it.

    ``args_preview`` is the redacted, truncated JSON of the call's arguments: a ticket never
    carries raw arguments. ``allow_session`` says whether "for this session" is a valid
    answer (the verdict has a grant key and is not ``always_ask``).
    """

    ticket_id: str
    task_id: str
    tool: str
    server: str
    domain: str
    reason: str
    rule_id: str
    source: str
    args_preview: str
    allow_session: bool
    created_at: datetime
    deadline: datetime


@dataclass(frozen=True)
class ApprovalResolution:
    """How an :class:`ApprovalTicket` ended."""

    ticket_id: str
    answer: ApprovalAnswer
    via: ApprovalVia
    resolved_at: datetime


class PermissionResourceResolver(Protocol):
    """Resolves the resources permission rules match on (``domain``, ``target``).

    ``url`` (the full address the call acts on) is informational: rules do not match on it,
    the approval classifier shows it to its model.

    Server-specific (the browser resolver reads URLs and snapshots); the engine stays
    server-neutral. An exception fails the check closed (``deny``).
    """

    def resources(
        self,
        check: PermissionCheck,
        state: Mapping[str, Any],
    ) -> Mapping[str, str]:
        """``{"domain": ..., "target": ..., "url": ...}``; ``{}`` (or a missing key) when unknown."""


class MemoryRenderer(Protocol):
    """Renders the persistent ``Memory`` context block for the current state.

    The engine's only view of persistent memory; the store, its files and tools stay in
    ``src/harness/``.
    """

    def render(self, state: Mapping[str, Any]) -> str:
        """The block text; ``""`` when there is nothing to show."""


@dataclass(frozen=True)
class MemoryEntry:
    """One persistent memory file: harness-owned frontmatter plus a markdown body."""

    #: Relative to the memory root, POSIX: ``"sites/ozon.ru.md"``.
    path: str
    kind: MemoryKind
    #: Normalized domain the entry applies to (it and its subdomains), or ``"*"``.
    scope: str
    status: MemoryStatus
    #: ``"user"`` or ``"agent:<task_id>"``.
    source: str
    #: One line for the index.
    description: str
    body: str
    #: ISO date of the last promotion to ``verified``; ``""`` when never verified.
    verified_at: str = ""
    #: Successful tasks that loaded the entry.
    uses: int = 0
    #: Consecutive blocked tasks that loaded the entry.
    failures: int = 0


class MemoryScopeResolver(Protocol):
    """Which memory scope the current state is in (the browser one reads the page URL)."""

    def scope(self, state: Mapping[str, Any]) -> str:
        """The normalized scope key; ``""`` when it is unknown."""


class MemoryContentPolicy(Protocol):
    """Refuses text that must never be persisted as memory (refs, selectors, secrets)."""

    def violation(self, text: str) -> str | None:
        """The reason ``text`` is refused, or ``None`` when it may be stored."""


def ladder_rank(stage: str) -> int:
    """Position of ``stage`` in :data:`LADDER_ORDER`; unknown stages rank above ``human``."""

    return LADDER_ORDER.index(stage) if stage in LADDER_ORDER else len(LADDER_ORDER)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RunBudget:
    """Caps of one task; ``None`` means no cap.

    Tokens are input plus output as the provider reports them. ``max_cost`` is in
    ``llm.currency`` and only checked while every model call of the task has a known cost.
    """

    max_model_calls: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None


@dataclass(frozen=True)
class ProbeStep:
    """One recorded-like tool call given inline (a stand probe); the tool name is user data."""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    #: MCP server of the tool; ``""``: whichever server exposes it.
    server: str = ""


@dataclass(frozen=True)
class PlaybackRequest:
    """What to replay: a stored recording by name (the step source factory loads it), or the
    inline ``steps`` of a probe (an anonymous recording; ``recording`` is then only a label)."""

    recording: str
    #: ``None`` keeps the recording's own answer mode.
    answer: PlaybackAnswer | None = None
    #: ``answer="output"``: regex of the output region to return (``None``: everything).
    select: str | None = None
    #: ``answer="output"``: regexes removed from the region before it is returned.
    ignore: tuple[str, ...] = ()
    #: Inline steps: no stored recording, no bindings, no checkpoints, no stats.
    steps: tuple[ProbeStep, ...] = ()


@dataclass(frozen=True)
class RunProfile:
    """How one task may spend. The default reproduces today's behaviour exactly."""

    playback: PlaybackRequest | None = None
    #: The highest ladder stage the task may reach (``human``: agent plus approvals).
    max_stage: LadderStage = "human"
    #: Carry the session's durable context into the task (a stand runs with ``False``).
    carry_context: bool = True
    #: Save a completed run as the recording of this name.
    record_as: str | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    #: Non-empty: tool calls outside these domains are denied (task scope, phase 3).
    allowed_domains: tuple[str, ...] = ()
    #: Free-form labels (job id, run id, trigger) for events and reports.
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCallRecord:
    """One model call a component other than the agent turn made (the retargeter)."""

    phase: str
    #: Model label (its id) for reports; ``""`` when unknown.
    model: str = ""
    #: Reported token fields (``ModelUsage.to_json_dict()``); empty when none were reported.
    usage: Mapping[str, int] = field(default_factory=dict)
    #: Known cost of the call; ``None`` without usage or without a price.
    cost: float | None = None
    tier: int = 0


@dataclass(frozen=True)
class TaskUsage:
    """Model accounting of one task, kept by the engine and returned with its result.

    ``cost`` is ``None`` once any model call had no usage or no price. ``by_stage`` and
    ``by_model`` map a ladder stage or a model label to ``{"model_calls", "input_tokens",
    "output_tokens", "cost"}`` (``cost`` omitted once unknown).
    """

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = 0.0
    by_stage: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    by_model: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: The highest ladder stage the task reached.
    stage_reached: LadderStage = "agent"

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


#: Emits one event through the engine's emitter: ``(event_type, payload)``.
EventCallback = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class StepView:
    """What a step source may read before and after a step: the engine's page view."""

    task: str
    turn: int
    #: The latest ``ToolResult`` (``name``, ``status``, ``content``, ``error``).
    tool_result: Mapping[str, Any]
    #: Kept read-only outputs per server (``BrowserState.snapshots``).
    snapshots: Mapping[str, str]
    #: Tool name -> ``{"server", "read_only", "schema_digest"}`` of the session's tools.
    tool_schemas: Mapping[str, Mapping[str, Any]]
    #: The task's model accounting so far (for the remaining budget).
    usage: TaskUsage = field(default_factory=TaskUsage)


@dataclass(frozen=True)
class StepProposal:
    """The next tool call a step source proposes; it runs through the normal pipeline."""

    #: ``{"name", "args"}`` with the references rebound to the current page.
    request: ToolRequest
    stage: Literal["playback", "retarget"]
    #: 1-based index of the step in the recording.
    step: int
    #: The recorded plan step of this call; moves ``current_step``.
    plan_step: int | None = None
    #: ``none`` (no references), ``rebound`` (references rebound) or ``retargeted``.
    binding: str = "none"


@dataclass(frozen=True)
class StepFinal:
    """The step source finished the task itself."""

    final_answer: str
    stage: Literal["playback", "retarget"] = "playback"


@dataclass(frozen=True)
class StepHandoff:
    """The source gives up; the model drives the rest of the task (sticky)."""

    #: Model-facing; becomes one ``[harness]`` note.
    reason: str
    kind: StepHandoffKind
    step: int | None = None
    #: Short operator-facing detail for ``playback.diverged`` (an element label at most,
    #: never a page excerpt; truncated to ``replay.detail_max_chars``).
    detail: str = ""


StepOutcome = StepProposal | StepFinal | StepHandoff


@runtime_checkable
class StepSource(Protocol):
    """Proposes the task's steps before the model is asked (playback of a recording)."""

    @property
    def info(self) -> Mapping[str, Any]:
        """``{"recording", "steps", "answer"}`` for ``playback.started``."""

    @property
    def usage(self) -> Sequence[ModelCallRecord]:
        """Every model call the source made so far (retargets), for budgets and reports."""

    def plan(self) -> list[PlanStep] | None:
        """The recorded plan (skips the planner call), or ``None`` to plan as usual."""

    async def next(self, view: StepView) -> StepOutcome:
        """The next step, the final answer, or a handoff to the model."""

    def observe(
        self,
        proposal: StepProposal,
        result: Mapping[str, Any],
        view: StepView,
        *,
        blocked: str = "",
    ) -> None:
        """Record how ``proposal`` went; ``blocked`` is the reason a guard, hook or
        permission stopped it before it ran (``result`` is then empty)."""


@runtime_checkable
class StepSourceFactory(Protocol):
    """Session-scoped (like hooks and permissions); builds one source per task."""

    def build(
        self,
        request: PlaybackRequest,
        *,
        task: str,
        budget: RunBudget,
        max_stage: LadderStage,
        emit: EventCallback,
    ) -> StepSource:
        """A source over ``request``; raises when the recording cannot be loaded."""


@dataclass(frozen=True)
class Binding:
    """A recorded argument value that referenced a page element, and how to find it again."""

    #: JSON path inside the arguments, found by value (never by an argument name).
    path: tuple[str | int, ...]
    #: The recorded ephemeral reference.
    value: str
    #: Opaque to everyone but the playback adapter that wrote it.
    descriptor: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BindResult:
    """Arguments with every binding rebound, or ``None`` (a ``binding`` divergence)."""

    args: Mapping[str, Any] | None
    detail: str = ""


@runtime_checkable
class PlaybackAdapter(Protocol):
    """Reads one family of tool output formats for recording and playback (name-free)."""

    def describe(self, args: Mapping[str, Any], page_texts: Sequence[str]) -> list[Binding]:
        """The argument values that are element references on the page before the step."""

    def rebind(
        self,
        args: Mapping[str, Any],
        bindings: Sequence[Binding],
        page_texts: Sequence[str],
        *,
        verify: bool = False,
    ) -> BindResult:
        """``args`` with each binding replaced by its current reference.

        ``verify=True`` only checks that the values already at the binding paths are live
        references of the current page (a retargeted call) and returns ``args`` unchanged.
        """

    def checkpoint(self, text: str) -> Mapping[str, Any]:
        """A fingerprint of a tool output."""

    def similarity(self, recorded: Mapping[str, Any], current: Mapping[str, Any]) -> float:
        """``0..1``; how much ``current`` looks like the recorded fingerprint."""

    def region(self, text: str, select: str | None, ignore: Sequence[str]) -> str:
        """The normalized output region an ``output`` answer returns."""


@dataclass(frozen=True)
class TaskRequest:
    """One task as the agent runs it: everything the engine needs, nothing else.

    Built by the session for every user task and handed unchanged through
    ``GoalRunner`` to ``Agent.run``. ``goal_id`` is ``task_id`` by construction.
    """

    task: str
    task_id: str
    session_id: str | None = None
    #: Durable context carried from the previous task plus the task-local resets.
    state_overrides: Mapping[str, Any] = field(default_factory=dict)
    #: Turn budget; ``None`` keeps ``settings.loop.turn_cap``.
    turn_cap: int | None = None
    compress_tools: bool = False
    #: Playback, ladder cap, budget and recording of this task; the default changes nothing.
    profile: RunProfile = field(default_factory=RunProfile)

    @property
    def goal_id(self) -> str:
        return self.task_id


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
    "LADDER_ORDER",
    "AgentDecision",
    "ApprovalAnswer",
    "ApprovalJudgeMode",
    "ApprovalResolution",
    "ApprovalTicket",
    "ApprovalVia",
    "RECORDING_NAME_PATTERN",
    "BindResult",
    "Binding",
    "CompactToolObservation",
    "CompletionStatus",
    "EventCallback",
    "GoalStatus",
    "goal_status_from_completion",
    "HookDecision",
    "HookEvent",
    "HookEventName",
    "HookHandler",
    "HookResult",
    "LadderStage",
    "ladder_rank",
    "MemoryContentPolicy",
    "MemoryEntry",
    "MemoryKind",
    "MemoryRenderer",
    "MemoryScopeResolver",
    "MemoryStatus",
    "ModelCallRecord",
    "PermissionCheck",
    "PermissionDecision",
    "PermissionMode",
    "PermissionResourceResolver",
    "PermissionSource",
    "PermissionVerdict",
    "PlanStep",
    "PlaybackAdapter",
    "PlaybackAnswer",
    "PlaybackRequest",
    "ProbeStep",
    "PolicyDecision",
    "PolicyEvent",
    "RecoveryCounters",
    "RunBudget",
    "RunProfile",
    "StepFinal",
    "StepHandoff",
    "StepHandoffKind",
    "StepOutcome",
    "StepProposal",
    "StepSource",
    "StepSourceFactory",
    "StepView",
    "StopReason",
    "TaskRequest",
    "TaskUsage",
    "Tool",
    "ToolDef",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
]
