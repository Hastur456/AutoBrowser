"""Model-based approval judgments: the model's own approval_request and the classifier."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.approval import (
    APPROVAL_ARGUMENT,
    ApprovalClassifier,
    ApprovalJudge,
    offer_approval_argument,
    split_approval_request,
)
from src.agent_loop.execution.loop import AgentLoopEngine, AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.config import PermissionRule, PermissionsSettings
from src.contracts import PermissionCheck, PermissionVerdict, Tool, ToolRequest
from src.harness.permissions import PermissionEngine
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelResponse
from tests.test_agent_loop_hooks import DONE, PLAN, CountingModel, tool_messages

PAY_REASON = "Оплата заказа на 9 826 ₽ (2 товара) на ozon.ru"


# --------------------------------------------------------------------------- helpers


class ReadOnlyTool:
    """A tool whose server declared ``readOnlyHint`` (like browser_snapshot)."""

    name = "look"
    description = "Read the page."
    input_schema = {"type": "object", "properties": {"depth": {"type": "integer"}}}
    annotations = {"readOnlyHint": True}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, args: dict[str, Any]) -> str:
        self.calls.append(dict(args))
        return "page"


class Pay:
    """A state-changing ``pay`` tool (no annotations) that records its arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def tool(self) -> Tool:
        async def pay(**kwargs: Any) -> str:
            self.calls.append(dict(kwargs))
            return "paid"

        return Tool(
            name="pay",
            func=pay,
            description="Click the pay button.",
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        )


class RecordingModel(CountingModel):
    """Scripted acting model that also records the tool schemas it was offered."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.offered: list[list[Any]] = []

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.offered.append(list(kwargs.get("tools") or []))
        return await super().complete(messages, **kwargs)


class Classifier:
    """Scripted classifier model: answers, an exception, or a hang."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.prompts.append(str(messages[-1].content))
        answer = self.answers.pop(0) if self.answers else {"approval": False, "reason": "ok"}
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.sleep(10)
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return ModelResponse(content=content)


class Human:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[ToolRequest, str, PermissionVerdict]] = []

    async def __call__(self, request: ToolRequest, reason: str, verdict: PermissionVerdict) -> Any:
        self.asked.append((request, reason, verdict))
        return self.answers.pop(0)


def call(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    return {
        "decision": "tool_call",
        "tool_request": {"name": name, "args": args, "reason": "Needed.", "id": call_id},
    }


def judge(mode: str, classifier: Any = None, timeout: float = 5.0) -> ApprovalJudge:
    return ApprovalJudge(
        mode=mode,  # type: ignore[arg-type]
        classifier=ApprovalClassifier(classifier, timeout_seconds=timeout) if classifier else None,
    )


def permissions(*rules: dict[str, Any], mode: str = "default") -> PermissionEngine:
    return PermissionEngine.from_settings(
        PermissionsSettings(mode=mode, rules=[PermissionRule(**rule) for rule in rules])
    )


async def run(
    responses: list[dict[str, Any]],
    *,
    tools: list[Any],
    approval: ApprovalJudge,
    human: Any = None,
    perms: PermissionEngine | None = None,
) -> tuple[AgentLoopResult, list[EventRecord], RecordingModel]:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    llm = RecordingModel(responses)
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=tools), event_emitter=emitter)
    resources = EngineResources.from_harness(
        harness, llm=llm, events=emitter, permissions=perms, approval=approval
    )
    result = await AgentLoopEngine(resources, human_input=human).run(
        "Купи куртку", task_id="task-1", goal_id="task-1", session_id="session-1", turn_cap=10
    )
    return result, list(sink.records), llm


def permission_events(records: list[EventRecord]) -> list[dict[str, Any]]:
    return [dict(r.payload) for r in records if r.type == "permission.decided"]


def approval_phases(records: list[EventRecord]) -> list[dict[str, Any]]:
    return [
        dict(r.payload)
        for r in records
        if r.type == "model.responded" and r.payload.get("phase") == "approval"
    ]


# --------------------------------------------------------------------------- the argument


def test_the_argument_is_offered_only_on_state_changing_tools() -> None:
    pay, look = Pay().tool(), ReadOnlyTool()

    defs, colliding = offer_approval_argument([pay, look])

    by_name = {d.name: d for d in defs}
    assert APPROVAL_ARGUMENT in by_name["pay"].input_schema["properties"]
    assert by_name["pay"].input_schema["required"] == ["target"]  # optional, never required
    assert APPROVAL_ARGUMENT not in by_name["look"].input_schema["properties"]
    assert APPROVAL_ARGUMENT not in pay.input_schema["properties"]  # the tool is not mutated
    assert colliding == frozenset()


def test_a_tool_that_owns_the_argument_name_keeps_it() -> None:
    schema = {"type": "object", "properties": {APPROVAL_ARGUMENT: {"type": "boolean"}}}
    own = Tool(name="own", func=lambda **_: None, input_schema=schema)

    defs, colliding = offer_approval_argument([own, Tool(name="bare", func=lambda **_: None)])

    assert defs[0].input_schema == schema
    assert defs[1].input_schema["properties"][APPROVAL_ARGUMENT]["type"] == "string"
    request = {"name": "own", "args": {APPROVAL_ARGUMENT: True}}
    assert split_approval_request(request, colliding) == (request, "")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (PAY_REASON, PAY_REASON),
        (True, "The model asked for approval of pay."),
        ("", ""),
        (None, ""),
        (False, ""),
        ("false", ""),
    ],
)
def test_split_removes_the_argument_and_returns_the_reason(value: Any, reason: str) -> None:
    request = {"name": "pay", "args": {"target": "e1", APPROVAL_ARGUMENT: value}, "id": "c1"}

    cleaned, asked = split_approval_request(request)

    assert cleaned == {"name": "pay", "args": {"target": "e1"}, "id": "c1"}
    assert asked == reason
    assert APPROVAL_ARGUMENT in request["args"]  # the input is not mutated


# --------------------------------------------------------------------------- the engine


def test_a_model_or_classifier_ask_is_always_asked_and_never_lifts_a_deny() -> None:
    perms = permissions(
        {"id": "no-pay", "decision": "deny", "tool": "pay"},
        {"id": "ok", "decision": "allow", "tool": "other"},
    )
    model = PermissionCheck(tool="other", model_ask_reason=PAY_REASON)
    perms.grant(("", "other", ""))

    verdict = perms.evaluate(model)
    assert (verdict.decision, verdict.source, verdict.reason) == ("ask", "model", PAY_REASON)
    assert verdict.always_ask and verdict.grant_key is None
    classifier = perms.evaluate(PermissionCheck(tool="other", classifier_ask_reason="Pays."))
    assert (classifier.decision, classifier.source) == ("ask", "classifier")
    denied = perms.evaluate(PermissionCheck(tool="pay", model_ask_reason=PAY_REASON))
    assert (denied.decision, denied.rule_id) == ("deny", "no-pay")


@pytest.mark.parametrize(("mode", "decision"), [("bypass", "ask"), ("dont_ask", "deny")])
def test_judge_asks_follow_the_always_ask_mode_logic(mode: str, decision: str) -> None:
    verdict = permissions(mode=mode).evaluate(
        PermissionCheck(tool="pay", model_ask_reason=PAY_REASON)
    )
    assert verdict.decision == decision


# --------------------------------------------------------------------------- model signal


@pytest.mark.asyncio
async def test_the_models_approval_request_asks_the_human_and_is_stripped() -> None:
    pay = Pay()
    human = Human("once")

    result, records, llm = await run(
        [PLAN, call("pay", "c1", target="e7", **{APPROVAL_ARGUMENT: PAY_REASON}), DONE],
        tools=[pay.tool()],
        approval=judge("model"),
        human=human,
    )

    assert result.status == "done"
    assert pay.calls == [{"target": "e7"}]
    ((request, reason, verdict),) = human.asked
    assert request["args"] == {"target": "e7"}
    assert reason == PAY_REASON
    assert (verdict.source, verdict.always_ask) == ("model", True)
    assert permission_events(records)[0]["source"] == "model"
    # The model was offered the argument; nothing downstream saw it.
    offered = {d.name: d for d in llm.offered[-1]}
    assert APPROVAL_ARGUMENT in offered["pay"].input_schema["properties"]
    assert APPROVAL_ARGUMENT not in json.dumps(
        [dict(r.payload) for r in records if r.type in {"action.proposed", "tool.started"}]
    )
    assert APPROVAL_ARGUMENT not in json.dumps(result.state.last_args)


@pytest.mark.asyncio
async def test_without_an_approval_request_the_call_runs_unasked() -> None:
    pay, human = Pay(), Human()

    result, _, _ = await run(
        [PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[pay.tool()],
        approval=judge("model"),
        human=human,
    )

    assert result.status == "done" and pay.calls == [{"target": "e7"}] and human.asked == []


@pytest.mark.asyncio
async def test_a_refused_model_ask_ends_the_task_blocked() -> None:
    pay = Pay()

    result, _, _ = await run(
        [PLAN, call("pay", "c1", target="e7", **{APPROVAL_ARGUMENT: PAY_REASON}), DONE],
        tools=[pay.tool()],
        approval=judge("model"),
        human=Human("deny"),
    )

    assert result.status == "blocked" and pay.calls == []
    assert "human approval was denied for pay" in result.final_answer


@pytest.mark.asyncio
async def test_a_model_ask_without_a_human_is_a_deny_the_model_reads() -> None:
    pay = Pay()

    result, _, _ = await run(
        [PLAN, call("pay", "c1", target="e7", **{APPROVAL_ARGUMENT: PAY_REASON}), DONE],
        tools=[pay.tool()],
        approval=judge("model"),
        perms=permissions(mode="dont_ask"),
    )

    assert result.status == "done" and pay.calls == []
    assert "permission mode dont_ask" in tool_messages(result)[0]


@pytest.mark.asyncio
async def test_with_the_judge_off_nothing_is_offered_or_stripped() -> None:
    pay = Pay()

    result, _, llm = await run(
        [PLAN, call("pay", "c1", target="e7", **{APPROVAL_ARGUMENT: PAY_REASON}), DONE],
        tools=[pay.tool()],
        approval=ApprovalJudge(),
        human=Human(),
    )

    assert result.status == "done"
    assert pay.calls == [{"target": "e7", APPROVAL_ARGUMENT: PAY_REASON}]
    assert all(
        APPROVAL_ARGUMENT not in json.dumps(getattr(t, "input_schema", {}))
        for t in llm.offered[-1]
    )


# --------------------------------------------------------------------------- classifier


@pytest.mark.asyncio
async def test_the_classifier_asks_the_human_with_its_reason() -> None:
    pay = Pay()
    classifier = Classifier({"approval": True, "reason": PAY_REASON})
    human = Human("once")

    result, records, _ = await run(
        [PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[pay.tool()],
        approval=judge("classifier", classifier),
        human=human,
    )

    assert result.status == "done" and pay.calls == [{"target": "e7"}]
    ((_, reason, verdict),) = human.asked
    assert (reason, verdict.source, verdict.always_ask) == (PAY_REASON, "classifier", True)
    assert approval_phases(records) == [
        {"phase": "approval", "tool": "pay", "needs_approval": True, "error": ""}
    ]
    (prompt,) = classifier.prompts
    assert "Task: Купи куртку" in prompt
    assert "Tool: pay" in prompt and "Click the pay button." in prompt
    assert '"target": "e7"' in prompt


@pytest.mark.asyncio
async def test_a_safe_verdict_runs_the_call_unasked() -> None:
    pay, human = Pay(), Human()

    result, _, _ = await run(
        [PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[pay.tool()],
        approval=judge("classifier", Classifier({"approval": False, "reason": "Opens cart."})),
        human=human,
    )

    assert result.status == "done" and pay.calls == [{"target": "e7"}] and human.asked == []


@pytest.mark.parametrize(
    "answer", [RuntimeError("provider down"), "not json", {"reason": "no verdict"}, "hang"]
)
@pytest.mark.asyncio
async def test_a_failing_classifier_asks(answer: Any) -> None:
    pay, human = Pay(), Human("deny")

    result, records, _ = await run(
        [PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[pay.tool()],
        approval=judge("classifier", Classifier(answer), timeout=0.05),
        human=human,
    )

    assert result.status == "blocked" and pay.calls == []
    ((_, reason, _),) = human.asked
    assert "could not judge pay" in reason
    assert approval_phases(records)[0]["error"]


@pytest.mark.asyncio
async def test_the_classifier_skips_read_only_tools_and_allow_rules() -> None:
    look, pay = ReadOnlyTool(), Pay()
    classifier = Classifier()
    perms = permissions({"id": "pay-ok", "decision": "allow", "tool": "pay"})

    result, _, _ = await run(
        [PLAN, call("look", "c1"), call("pay", "c2", target="e7"), DONE],
        tools=[look, pay.tool()],
        approval=judge("classifier", classifier),
        perms=perms,
    )

    assert result.status == "done" and look.calls and pay.calls
    assert classifier.prompts == []


@pytest.mark.asyncio
async def test_a_deny_rule_wins_without_a_classifier_call() -> None:
    classifier = Classifier({"approval": True, "reason": "x"})

    result, _, _ = await run(
        [PLAN, call("pay", "c1", target="e7"), DONE],
        tools=[Pay().tool()],
        approval=judge("classifier", classifier),
        perms=permissions({"id": "no-pay", "decision": "deny", "tool": "pay"}),
    )

    assert result.status == "done" and classifier.prompts == []


@pytest.mark.asyncio
async def test_both_a_model_ask_skips_the_classifier() -> None:
    classifier = Classifier()
    human = Human("once")

    await run(
        [PLAN, call("pay", "c1", target="e7", **{APPROVAL_ARGUMENT: PAY_REASON}), DONE],
        tools=[Pay().tool()],
        approval=judge("both", classifier),
        human=human,
    )

    assert classifier.prompts == []
    assert human.asked[0][2].source == "model"


# --------------------------------------------------------------------------- settings


def test_from_settings_builds_the_configured_judge() -> None:
    built: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> str:
        built.append(kwargs)
        return "classifier-llm"

    off = ApprovalJudge.from_settings(PermissionsSettings(), llm="llm", llm_factory=factory)
    assert (off.mode, off.classifier, off.model_signal, off.classifies) == ("off", None, False, False)

    model = ApprovalJudge.from_settings(
        PermissionsSettings(approval_judge="model"), llm="llm", llm_factory=factory
    )
    assert model.model_signal and not model.classifies

    both = ApprovalJudge.from_settings(
        PermissionsSettings(approval_judge="both", classifier_model="small:1b"),
        llm="llm",
        llm_factory=factory,
    )
    assert both.model_signal and both.classifies
    assert both.classifier is not None and both.classifier._llm == "classifier-llm"
    assert built == [{"model": "small:1b", "temperature": 0.0}]

    reuse = ApprovalJudge.from_settings(
        PermissionsSettings(approval_judge="classifier"), llm="llm", llm_factory=factory
    )
    assert reuse.classifier is not None and reuse.classifier._llm == "llm"


def test_a_bare_yaml_off_is_accepted() -> None:
    assert PermissionsSettings(approval_judge=False).approval_judge == "off"  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PermissionsSettings(approval_judge="always")  # type: ignore[arg-type]
