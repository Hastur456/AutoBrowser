"""Scenario eval harness for the engine-native agent loop."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from src.llm import ModelResponse
from src.messages import Message

from src.agent_loop.comparison import Finding
from src.agent_loop.context import ContextAssembler, PromptSection
from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.loop import Agent
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.replay import (
    TraceSummary,
    iter_action_sequence,
    print_action_sequence,
    summarize_trace,
)
from src.browser.errors import BROWSER_ERROR_ACTION_FAILED, BROWSER_ERROR_INVALID_REF
from src.browser.permissions import BrowserResourceResolver
from src.config import PermissionsSettings, SkillsSettings
from src.contracts import (
    ApprovalAnswer,
    PermissionVerdict,
    TaskRequest,
    Tool,
    ToolRequest,
    ToolResult,
    goal_status_from_completion,
)
from src.harness.permissions import PermissionEngine
from src.harness.normalization import SchemaArgsNormalizer
from src.harness.runtime import BrowserHarness
from src.harness.skill_tool import skill_tools
from src.harness.skills import (
    BROWSER_CAPABILITY,
    SkillStore,
    always_sections,
    builtin_skills_root,
    catalog_section,
)
from src.harness.tools import ToolRegistry

_INVALID_REF_PATTERN = re.compile(
    r"\bRef\s+[A-Za-z][A-Za-z0-9_-]*\s+not\s+found\b",
    re.IGNORECASE,
)
_REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
_REF_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class _FakeBrowserTools:
    """Replay browser tools from a deterministic sequence of snapshots.

    Doubles as a tool provider (``get_tools``) and a tool-call normalizer
    (``normalize_request``/``normalize_result``) for :class:`ToolRegistry`, so eval
    scenarios can drive the engine's browser tool-call path without Chrome/CDP.
    """

    def __init__(self, snapshots: list[str]) -> None:
        if not snapshots:
            raise ValueError("_FakeBrowserTools requires at least one snapshot.")

        self._snapshots = list(snapshots)
        self._snapshot_index = 0
        self._tools = self._build_tools()
        self._tool_names = frozenset(tool.name for tool in self._tools)
        self._ref_tools = frozenset(
            tool.name for tool in self._tools if "ref" in tool.input_schema.get("properties", {})
        )

    async def get_tools(self) -> Sequence[Any]:
        return list(self._tools)

    def normalize_request(
        self, request: ToolRequest, state: Any, tools: Any = None
    ) -> ToolRequest:
        _ = tools
        normalized_request = dict(request)
        args = dict(request.get("args") or {})
        requested_name = str(request.get("name", "") or "").strip()
        # Only its own element tools (those whose schema takes a ``ref``) are adapted.
        if requested_name in self._ref_tools:
            ref = self._ref_from_args(args)
            if ref:
                args.setdefault("ref", ref)
                args.setdefault("target", ref)

        normalized_request["args"] = args
        return normalized_request

    def normalize_result(self, result: ToolResult) -> ToolResult:
        normalized_result = dict(result)
        tool_name = str(normalized_result.get("name", "") or "")
        if (
            normalized_result.get("status") != "error"
            or tool_name not in self._tool_names
            or normalized_result.get("error_code")
        ):
            return normalized_result

        if self._has_invalid_ref_error(normalized_result):
            normalized_result["error_code"] = BROWSER_ERROR_INVALID_REF
        else:
            normalized_result["error_code"] = BROWSER_ERROR_ACTION_FAILED
        return normalized_result

    @staticmethod
    def _has_invalid_ref_error(result: ToolResult) -> bool:
        payload = str(result.get("error", "") or result.get("content", "") or "")
        return bool(_INVALID_REF_PATTERN.search(payload))

    def _build_tools(self) -> list[Tool]:
        async def browser_navigate(url: str) -> str:
            """Navigate to a URL in the fake browser."""

            self._advance_snapshot()
            return f"Navigated to {url}."

        async def browser_snapshot(depth: int | None = None) -> str:
            """Return the current fake browser snapshot."""

            _ = depth
            return self._current_snapshot()

        async def browser_click(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Click an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Clicked ref {resolved_ref}."

        async def browser_type(
            text: str,
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Type text into an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Typed into ref {resolved_ref}: {text}"

        async def browser_hover(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Hover an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Hovered ref {resolved_ref}."

        return [
            self._tool(browser_navigate, {"url": {"type": "string"}}, required=("url",)),
            # Read-only like the real Playwright snapshot tool: its output is the kept snapshot.
            self._tool(browser_snapshot, {"depth": {"type": "integer"}}, read_only=True),
            self._tool(
                browser_click,
                {
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
            ),
            self._tool(
                browser_type,
                {
                    "text": {"type": "string"},
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
                required=("text",),
            ),
            self._tool(
                browser_hover,
                {
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
            ),
        ]

    def _tool(
        self,
        func: Any,
        properties: dict[str, Any],
        *,
        required: Sequence[str] = (),
        read_only: bool = False,
    ) -> Tool:
        """Wrap one async fake-browser function into a neutral ``Tool``."""

        return Tool(
            name=func.__name__,
            description=str(func.__doc__ or "").strip(),
            input_schema={
                "type": "object",
                "properties": dict(properties),
                **({"required": list(required)} if required else {}),
            },
            func=func,
            annotations={"readOnlyHint": True} if read_only else None,
        )

    def _current_snapshot(self) -> str:
        return self._snapshots[self._snapshot_index]

    def _advance_snapshot(self) -> None:
        if self._snapshot_index < len(self._snapshots) - 1:
            self._snapshot_index += 1

    def _assert_ref_exists(self, ref: str) -> None:
        if ref not in self._snapshot_refs(self._current_snapshot()):
            raise ValueError(f"Ref {ref} not found")

    def _require_ref(self, *, ref: str | None, target: str | None) -> str:
        resolved_ref = str(ref or "").strip()
        if resolved_ref:
            return resolved_ref

        resolved_target = str(target or "").strip()
        if self._looks_like_ref(resolved_target):
            return resolved_target

        raise ValueError("Fake browser action requires a ref or ref-like target.")

    def _ref_from_args(self, args: dict[str, Any]) -> str:
        ref = str(args.get("ref", "") or "").strip()
        if ref:
            return ref

        target = str(args.get("target", "") or "").strip()
        return target if self._looks_like_ref(target) else ""

    def _snapshot_refs(self, snapshot: str) -> set[str]:
        return {match.group(1) for match in _REF_PATTERN.finditer(snapshot)}

    def _looks_like_ref(self, value: str) -> bool:
        return bool(_REF_VALUE_PATTERN.fullmatch(value))


class ScriptExhaustedError(IndexError):
    """A strict :class:`FakeChatModel` was asked for more responses than its script has."""


class FakeChatModel:
    """Deterministic provider-neutral model returning scenario responses in order.

    ``complete`` replays the next scripted response verbatim as ``content``
    (plan- and agent-turn JSON alike), so the eval scenarios drive the engine
    over the neutral ``ChatModel`` contract end to end.

    By default the script cycles, so an extra model call gets the first response again.
    ``strict=True`` raises :class:`ScriptExhaustedError` instead and sets ``exhausted``: an
    engine change that adds a model call then fails the scenario rather than replaying a
    plan as an agent turn.
    """

    def __init__(self, responses: list[str], *, strict: bool = False) -> None:
        self._responses = list(responses)
        self._index = 0
        self._strict = strict
        #: True once a strict script ran out of responses.
        self.exhausted = False
        #: Characters of every prompt sent (message contents), for prompt-size comparisons.
        self.prompt_chars = 0
        #: Characters of every request: messages with their tool calls plus the tool schemas.
        self.request_chars = 0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Any] = (),
        **params: Any,
    ) -> ModelResponse:
        if not self._responses:
            raise IndexError("FakeChatModel has no scripted responses.")
        self.prompt_chars += sum(len(str(message.content or "")) for message in messages)
        self.request_chars += _request_chars(messages, tools)
        if self._strict and self._index >= len(self._responses):
            self.exhausted = True
            raise ScriptExhaustedError(
                f"FakeChatModel script exhausted after {len(self._responses)} responses."
            )
        content = self._responses[self._index % len(self._responses)]
        self._index += 1
        return ModelResponse(content=content, finish_reason="stop")


def _request_chars(messages: Sequence[Message], tools: Sequence[Any]) -> int:
    """Size of one model request: message contents, their tool calls and the tool schemas."""

    total = 0
    for message in messages:
        total += len(str(message.content or ""))
        if message.tool_calls:
            total += len(_canonical_json([call.to_dict() for call in message.tool_calls]))
    for tool in tools:
        total += len(_canonical_json(asdict(tool) if is_dataclass(tool) else str(tool)))
    return total


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class EvalAssertions:
    """Assertions configured by a scenario fixture."""

    expected_terminal_status: str = "completed"
    final_answer_contains: list[str] = field(default_factory=list)
    max_tool_calls: int | None = None
    max_repeated_actions: int | None = None
    max_policy_blocks: int | None = None


@dataclass(frozen=True)
class EvalScenario:
    """One replayable browser-agent eval scenario."""

    name: str
    task: str
    model_responses: list[str]
    browser_snapshots: list[str]
    assertions: EvalAssertions
    turn_cap: int = 25
    #: Scenario permissions; evals are headless, so the default mode is ``dont_ask``.
    permissions: PermissionsSettings = field(
        default_factory=lambda: PermissionsSettings(mode="dont_ask")
    )
    #: Scripted human answers to approval prompts, in order (then ``deny``).
    human_answers: list[str] = field(default_factory=list)
    #: Fail on a model call past the script instead of cycling (see :class:`FakeChatModel`).
    strict: bool = False


@dataclass(frozen=True)
class EvalResult:
    """Scenario result with metrics and compact trace output."""

    scenario_name: str
    summary: TraceSummary
    action_sequence: str
    final_state: dict[str, Any]
    #: Characters of all model prompts; not part of :meth:`metrics` (the baseline).
    prompt_chars: int = 0
    #: Characters of all model requests, tool schemas included (see ``_request_chars``).
    request_chars: int = 0
    #: A strict scenario asked the model for more responses than it scripts.
    script_exhausted: bool = False
    #: Tool calls that finished with ``status: error``.
    error_count: int = 0
    #: Approval prompts raised (``approval.requested``).
    approval_request_count: int = 0
    #: Observations compiled after tool calls (``observation.compiled``).
    observation_count: int = 0
    #: ``sha256:`` of the proposed calls as ``(tool, sorted argument keys)``, see
    #: :func:`action_signature`.
    action_signature: str = ""

    def metrics(self) -> dict[str, Any]:
        """The v1 baseline metrics (``agent_loop_v1.json`` plus ``final_answer``)."""

        return {
            "terminal_status": self.summary.terminal_status,
            "final_answer": self.summary.final_answer,
            "model_turn_count": self.summary.model_turn_count,
            "tool_call_count": self.summary.tool_call_count,
            "policy_block_count": self.summary.policy_block_count,
            "repeated_action_count": self.summary.repeated_action_count,
            "trace_complete": self.summary.trace_complete,
        }

    def baseline_metrics(self) -> dict[str, Any]:
        """The v2 baseline record: v1 metrics plus outcome counters, the action signature and
        the request size. ``final_answer`` has its whitespace normalized."""

        return {
            **self.metrics(),
            "final_answer": " ".join(self.summary.final_answer.split()),
            "approval_request_count": self.approval_request_count,
            "error_count": self.error_count,
            "observation_count": self.observation_count,
            "action_signature": self.action_signature,
            "request_chars": self.request_chars,
        }


@dataclass(frozen=True)
class ScenarioRun:
    """One scenario run collected by :func:`collect_eval_metrics`."""

    scenario: EvalScenario
    result: EvalResult
    #: Failed configured assertions, empty when the scenario met them all.
    problems: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.scenario.name


def action_signature(events: Sequence[EventRecord]) -> str:
    """``sha256:`` over the proposed calls as ``(tool, sorted argument keys)``.

    Argument values are left out (the scenario fixes them); the keys catch a changed schema
    or normalization even when every counter stays the same.
    """

    sequence = [[action.name, sorted(action.args)] for action in iter_action_sequence(list(events))]
    digest = hashlib.sha256(_canonical_json(sequence).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _tool_error_count(events: Sequence[EventRecord]) -> int:
    count = 0
    for event in events:
        if event.type != "tool.finished":
            continue
        result = event.payload.get("tool_result")
        if isinstance(result, dict) and result.get("status") == "error":
            count += 1
    return count


def _event_count(events: Sequence[EventRecord], event_type: str) -> int:
    return sum(1 for event in events if event.type == event_type)


def load_scenario(path: Path) -> EvalScenario:
    """Load an eval scenario from YAML."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assertions = data.get("assertions") or {}
    return EvalScenario(
        name=str(data["name"]),
        task=str(data["task"]),
        model_responses=[
            _json_response(response)
            for response in (data.get("model", {}).get("responses") or [])
        ],
        browser_snapshots=[str(item) for item in data.get("browser", {}).get("snapshots") or []],
        assertions=EvalAssertions(
            expected_terminal_status=str(
                assertions.get("expected_terminal_status", "completed")
            ),
            final_answer_contains=[
                str(item) for item in assertions.get("final_answer_contains", [])
            ],
            max_tool_calls=assertions.get("max_tool_calls"),
            max_repeated_actions=assertions.get("max_repeated_actions"),
            max_policy_blocks=assertions.get("max_policy_blocks"),
        ),
        turn_cap=int(data.get("turn_cap", 25) or 25),
        permissions=PermissionsSettings(**{"mode": "dont_ask", **(data.get("permissions") or {})}),
        human_answers=[str(item) for item in (data.get("human") or {}).get("answers") or []],
        strict=bool(data.get("strict", False)),
    )


def _scripted_human(answers: list[str]) -> Any:
    """Human-in-the-loop callback replaying ``answers``; ``deny`` once they run out."""

    remaining = list(answers)

    async def answer(
        request: ToolRequest,
        reason: str,
        verdict: PermissionVerdict,
    ) -> ApprovalAnswer:
        return remaining.pop(0) if remaining else "deny"  # type: ignore[return-value]

    return answer


def builtin_skill_store(settings: SkillsSettings | None = None) -> SkillStore:
    """The built-in skills of a browser session, from explicit (by default the code-default)
    skill settings — never the developer's config or skills folder."""

    return SkillStore.load(
        settings or SkillsSettings(),
        roots=[("builtin", builtin_skills_root())],
        capabilities={BROWSER_CAPABILITY},
    )


def _browser_sections() -> list[PromptSection]:
    """The built-in always-on skills of a browser session."""

    return always_sections(builtin_skill_store())


async def run_scenario(
    scenario: EvalScenario,
    *,
    memory: Any | None = None,
    skills: SkillStore | None = None,
) -> EvalResult:
    """Run one scenario against the engine-native execution loop.

    ``memory`` is an explicit ``Memory`` block renderer (a ``MemoryContext`` over a seed
    directory); evals never read the developer's memory or its settings. ``skills`` is an
    explicit store run as a session would run it: with ``enabled`` and a visible skill, the
    catalog and ``skill_view``; the always-on bodies either way. Without it, the built-in
    always-on bodies only (skills off).
    """
    skills_on = skills is not None and skills.settings.enabled and bool(skills.skills())

    sink = InMemoryEventSink()
    session_id = f"eval-{uuid4().hex}"
    task_id = f"task-{uuid4().hex}"
    emitter = EventEmitter(sink, session_id=session_id)
    provider = _FakeBrowserTools(scenario.browser_snapshots)
    llm = FakeChatModel(responses=scenario.model_responses, strict=scenario.strict)
    harness = BrowserHarness(
        llm=llm,
        # A browser scenario: the same always-on skills a session with a browser server gets.
        context_assembler=ContextAssembler(
            sections=(
                (catalog_section(skills) if skills_on else []) + always_sections(skills)
                if skills is not None
                else _browser_sections()
            )
        ),
        # Same schema normalizer the real session wires (see src/harness/mcp_setup.py).
        tool_registry=ToolRegistry(
            list(skill_tools(skills)) if skills is not None and skills_on else None,
            providers=[provider],
            normalizers=[SchemaArgsNormalizer(), provider],
        ),
        event_emitter=emitter,
    )
    # The session's browser permission wiring, over the scenario's rules and mode.
    resources = EngineResources.from_harness(
        harness,
        llm=llm,
        events=emitter,
        permissions=PermissionEngine.from_settings(
            scenario.permissions,
            resolver=BrowserResourceResolver(),
        ),
        memory=memory,
    )
    emitter.emit(
        "goal.started",
        source="agent_loop.evals",
        payload={"task": scenario.task},
        task_id=task_id,
        goal_id=task_id,
    )
    agent = Agent(resources, human_input=_scripted_human(scenario.human_answers))
    request = TaskRequest(
        task=scenario.task,
        task_id=task_id,
        session_id=session_id,
        turn_cap=scenario.turn_cap,
    )
    try:
        result = await agent.run(request)
    except Exception as exc:
        emitter.emit(
            "goal.failed",
            source="agent_loop.evals",
            payload={"task": scenario.task, "status": "failed", "error": exc},
            task_id=task_id,
            goal_id=task_id,
        )
        final_state: dict[str, Any] = {}
    else:
        final_state = {
            **dict(result.session_state),
            "final_answer": str(result.final_answer or ""),
            "decision": str(result.status or ""),
        }
        # The same mapping GoalRunner uses, so ``cancelled`` stays ``cancelled``.
        terminal = goal_status_from_completion(result.status) or "failed"
        emitter.emit(
            f"goal.{terminal}",
            source="agent_loop.evals",
            payload={
                "task": scenario.task,
                "status": terminal,
                "final_answer": final_state["final_answer"],
                "turns": result.turns,
            },
            task_id=task_id,
            goal_id=task_id,
        )
    events = sink.records
    summary = summarize_trace(events)
    return EvalResult(
        scenario_name=scenario.name,
        summary=summary,
        action_sequence=print_action_sequence(events),
        final_state=final_state,
        prompt_chars=llm.prompt_chars,
        request_chars=llm.request_chars,
        script_exhausted=llm.exhausted,
        error_count=_tool_error_count(events),
        approval_request_count=_event_count(events, "approval.requested"),
        observation_count=_event_count(events, "observation.compiled"),
        action_signature=action_signature(events),
    )


async def collect_eval_metrics(
    scenarios: Sequence[EvalScenario],
    *,
    memory_factory: Callable[[], Any] | None = None,
) -> list[ScenarioRun]:
    """Run every scenario and check its assertions without raising.

    A failed assertion or an exhausted script is reported on the returned
    :class:`ScenarioRun` rather than raised, so a caller can report every scenario.
    ``memory_factory`` builds a fresh ``Memory`` block renderer per scenario.
    """

    runs: list[ScenarioRun] = []
    for scenario in scenarios:
        memory = memory_factory() if memory_factory is not None else None
        result = await run_scenario(scenario, memory=memory)
        runs.append(
            ScenarioRun(
                scenario=scenario,
                result=result,
                problems=tuple(check_scenario_result(scenario, result)),
            )
        )
    return runs


#: The problem line of a strict scenario that ran out of scripted model responses.
SCRIPT_EXHAUSTED_PROBLEM = "the strict model script ran out of responses"


def check_scenario_result(scenario: EvalScenario, result: EvalResult) -> list[str]:
    """The configured scenario expectations the result misses, as readable lines."""

    summary = result.summary
    assertions = scenario.assertions
    problems: list[str] = []
    if result.script_exhausted:
        problems.append(SCRIPT_EXHAUSTED_PROBLEM)
    if summary.terminal_status != assertions.expected_terminal_status:
        problems.append(
            f"terminal_status: expected {assertions.expected_terminal_status!r},"
            f" got {summary.terminal_status!r}"
        )
    for expected_text in assertions.final_answer_contains:
        if expected_text not in summary.final_answer:
            problems.append(f"final_answer does not contain {expected_text!r}")
    limits = (
        ("tool_call_count", summary.tool_call_count, assertions.max_tool_calls),
        ("repeated_action_count", summary.repeated_action_count, assertions.max_repeated_actions),
        ("policy_block_count", summary.policy_block_count, assertions.max_policy_blocks),
    )
    for metric, actual, limit in limits:
        if limit is not None and actual > limit:
            problems.append(f"{metric}: {actual} exceeds the limit {limit}")
    if summary.trace_complete is not True:
        problems.append("trace_complete: the trace has no goal.started or no terminal event")
    return problems


def run_findings(runs: Sequence[ScenarioRun], *, suite: str) -> list[Finding]:
    """Comparison findings of the runs themselves, before any baseline is consulted.

    An exhausted strict script is ``script_exhausted``; every other failed scenario assertion
    is a ``regression`` of the ``assertions`` metric.
    """

    findings: list[Finding] = []
    for run in runs:
        if run.result.script_exhausted:
            findings.append(
                Finding("script_exhausted", suite, run.name, message=SCRIPT_EXHAUSTED_PROBLEM)
            )
        for problem in run.problems:
            if problem != SCRIPT_EXHAUSTED_PROBLEM:
                findings.append(
                    Finding("regression", suite, run.name, metric="assertions", message=problem)
                )
    return findings


def assert_scenario_result(scenario: EvalScenario, result: EvalResult) -> None:
    """Assert configured scenario expectations with compact trace diagnostics."""

    problems = check_scenario_result(scenario, result)
    assert not problems, "\n".join([*problems, str(result.metrics()), result.action_sequence])


def _json_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False)


__all__ = [
    "EvalAssertions",
    "EvalResult",
    "EvalScenario",
    "FakeChatModel",
    "ScenarioRun",
    "ScriptExhaustedError",
    "action_signature",
    "assert_scenario_result",
    "SCRIPT_EXHAUSTED_PROBLEM",
    "check_scenario_result",
    "collect_eval_metrics",
    "load_scenario",
    "run_findings",
    "run_scenario",
]
