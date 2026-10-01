"""PermissionEngine inside the native loop: order, non-terminal denies, approvals and grants."""

from __future__ import annotations

from typing import Any

import pytest

from src.config import PermissionRule, PermissionsSettings
from src.contracts import HookResult, PermissionVerdict, Tool, ToolRequest
from src.harness.hooks import HookEngine
from src.harness.permissions import PermissionEngine
from tests.test_agent_loop_hooks import (
    DONE,
    PLAN,
    EchoTool,
    hook,
    returning,
    run_engine,
    tool_messages,
    types_of,
)


def permissions(*rules: dict[str, Any], mode: str = "default") -> PermissionEngine:
    return PermissionEngine.from_settings(
        PermissionsSettings(mode=mode, rules=[PermissionRule(**rule) for rule in rules])
    )


def call(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    return {
        "decision": "tool_call",
        "tool_request": {"name": name, "args": args, "reason": "Needed.", "id": call_id},
    }


def permission_events(records: list[Any]) -> list[dict[str, Any]]:
    return [dict(r.payload) for r in records if r.type == "permission.decided"]


class Human:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, PermissionVerdict]] = []

    async def __call__(self, request: ToolRequest, reason: str, verdict: PermissionVerdict) -> Any:
        self.asked.append((reason, verdict))
        return self.answers.pop(0)


@pytest.mark.asyncio
async def test_a_deny_rule_skips_the_call_and_lets_the_model_continue() -> None:
    echo = EchoTool()
    perms = permissions({"id": "no-echo", "decision": "deny", "tool": "echo", "reason": "No echo."})

    result, records, llm = await run_engine(
        [PLAN, call("echo", "c1", text="secret-value"), call("other", "c2"), DONE],
        tools=echo.tools(),
        permissions=perms,
    )

    assert echo.calls == [{"other": {}}]
    assert result.status == "done"
    assert llm.calls == 4  # the deny was not terminal
    assert tool_messages(result)[0] == "echo\n\nNo echo."
    (denied, allowed) = permission_events(records)
    assert denied == {
        "tool": "echo",
        "server": "",
        "decision": "deny",
        "source": "rule",
        "rule_id": "no-echo",
        "mode": "default",
        "reason": "No echo.",
    }
    assert allowed["decision"] == "allow"
    assert "secret-value" not in str(permission_events(records))
    assert result.state.consecutive_failures == 0  # reset by the successful ``other`` call


@pytest.mark.asyncio
async def test_a_deny_is_counted_as_a_failure_and_the_state_records_the_rule() -> None:
    perms = permissions({"id": "no-echo", "decision": "deny", "tool": "echo"})

    result, _, _ = await run_engine(
        [PLAN, call("echo", "c1", text="a"), DONE],
        tools=EchoTool().tools(),
        permissions=perms,
    )

    assert result.state.consecutive_failures == 1
    assert result.state.policy_decision == "blocked"
    assert result.state.policy_event["rule_id"] == "no-echo"
    assert result.state.policy_event["source"] == "rule"


@pytest.mark.asyncio
async def test_a_hook_allow_cannot_lift_a_deny_rule() -> None:
    echo = EchoTool()
    hooks = HookEngine([hook("ok", "pre_tool_use", returning(HookResult(decision="allow")))])

    result, records, _ = await run_engine(
        [PLAN, call("echo", "c1", text="a"), DONE],
        hooks=hooks,
        tools=echo.tools(),
        permissions=permissions({"id": "no", "decision": "deny", "tool": "echo"}),
    )

    assert echo.calls == []
    turn = [t for t in types_of(records) if t not in {"model.requested", "model.responded"}]
    assert turn[:3] == ["action.proposed", "hook.decided", "permission.decided"]


@pytest.mark.asyncio
async def test_rules_see_the_arguments_after_a_hook_rewrite() -> None:
    echo = EchoTool()
    hooks = HookEngine(
        [hook("rw", "pre_tool_use", returning(HookResult(updated_input={"text": "forbidden"})))]
    )
    rule = {"id": "f", "decision": "deny", "tool": "echo", "args": {"text": "forbidden"}}

    result, _, _ = await run_engine(
        [PLAN, call("echo", "c1", text="harmless"), DONE],
        hooks=hooks,
        tools=echo.tools(),
        permissions=permissions(rule),
    )

    assert echo.calls == []
    assert "Denied by rule f." in tool_messages(result)[0]


@pytest.mark.asyncio
async def test_a_hook_ask_in_dont_ask_mode_is_a_non_terminal_deny() -> None:
    echo = EchoTool()
    human = Human()
    hooks = HookEngine(
        [hook("confirm", "pre_tool_use", returning(HookResult(decision="ask", reason="Confirm.")))]
    )

    result, records, _ = await run_engine(
        [PLAN, call("echo", "c1", text="a"), DONE],
        hooks=hooks,
        tools=echo.tools(),
        human_input=human,
        permissions=permissions(mode="dont_ask"),
    )

    assert echo.calls == [] and human.asked == []
    assert result.status == "done"
    assert "approval.requested" not in types_of(records)
    (event,) = permission_events(records)
    assert (event["decision"], event["source"], event["mode"]) == ("deny", "mode", "dont_ask")
    assert "Confirm." in tool_messages(result)[0]


@pytest.mark.asyncio
async def test_a_session_answer_grants_the_next_identical_ask() -> None:
    echo = EchoTool()
    human = Human("session")
    perms = permissions({"id": "confirm", "decision": "ask", "tool": "echo"})

    result, records, _ = await run_engine(
        [PLAN, call("echo", "c1", text="a"), call("echo", "c2", text="b"), DONE],
        tools=echo.tools(),
        human_input=human,
        permissions=perms,
    )

    assert echo.calls == [{"text": "a"}, {"text": "b"}]
    assert result.status == "done"
    assert len(human.asked) == 1
    reason, verdict = human.asked[0]
    assert reason == "Approval required by rule confirm."
    assert verdict.grant_key == ("", "echo", "")
    assert perms.grants == {("", "echo", "")}
    resolved = [dict(r.payload) for r in records if r.type == "approval.resolved"]
    assert resolved == [
        {"decision": "allow", "by": "human", "scope": "session"},
        {"decision": "allow", "by": "grant", "scope": "session"},
    ]
    assert [e["source"] for e in permission_events(records)] == ["rule", "grant"]


@pytest.mark.asyncio
async def test_a_session_answer_on_always_ask_is_only_once() -> None:
    echo = EchoTool()
    human = Human("session", "once")
    perms = permissions({"id": "js", "decision": "ask", "tool": "echo", "always_ask": True})

    result, records, _ = await run_engine(
        [PLAN, call("echo", "c1", text="a"), call("echo", "c2", text="b"), DONE],
        tools=echo.tools(),
        human_input=human,
        permissions=perms,
    )

    assert len(human.asked) == 2 and perms.grants == frozenset()
    assert result.status == "done"
    scopes = [r.payload["scope"] for r in records if r.type == "approval.resolved"]
    assert scopes == ["once", "once"]


@pytest.mark.asyncio
async def test_a_human_refusal_stays_terminal() -> None:
    echo = EchoTool()
    human = Human("deny")

    result, records, llm = await run_engine(
        [PLAN, call("echo", "c1", text="a"), DONE],
        tools=echo.tools(),
        human_input=human,
        permissions=permissions({"id": "confirm", "decision": "ask", "tool": "echo"}),
    )

    assert echo.calls == []
    assert result.status == "blocked"
    assert result.final_answer == "Blocked: human approval was denied for echo."
    assert llm.calls == 2
    requested = next(r.payload for r in records if r.type == "approval.requested")
    assert requested["rule_id"] == "confirm"


@pytest.mark.asyncio
async def test_without_an_engine_sensitive_names_still_ask_the_human() -> None:
    """The default ``EngineResources.permissions`` keeps the builtin marker rule."""

    bought: list[Any] = []

    async def purchase(**kwargs: Any) -> str:
        bought.append(kwargs)
        return "bought"

    human = Human(True)
    result, _, _ = await run_engine(
        [PLAN, call("purchase_item", "c1", sku="1"), DONE],
        tools=[Tool(name="purchase_item", func=purchase)],
        human_input=human,
    )

    assert bought == [{"sku": "1"}]
    assert result.status == "done"
    (reason, verdict) = human.asked[0]
    assert reason == "Tool requires human approval before use: purchase_item"
    assert (verdict.source, verdict.rule_id) == ("builtin", "sensitive-tool-name")


@pytest.mark.asyncio
async def test_read_only_mode_runs_only_annotated_tools() -> None:
    calls: list[str] = []

    class Annotated:
        def __init__(self, name: str, read_only: bool) -> None:
            self.name = name
            self.annotations = {"readOnlyHint": read_only}

        async def invoke(self, args: dict[str, Any]) -> str:
            calls.append(self.name)
            return self.name

    result, _, _ = await run_engine(
        [PLAN, call("look", "c1"), call("change", "c2"), DONE],
        tools=[Annotated("look", True), Annotated("change", False)],
        permissions=permissions(mode="read_only"),
    )

    assert calls == ["look"]
    assert result.status == "done"
    assert "read_only" in tool_messages(result)[1]


@pytest.mark.asyncio
async def test_unknown_tools_skip_permissions() -> None:
    result, records, _ = await run_engine(
        [PLAN, call("missing", "c1"), DONE],
        tools=EchoTool().tools(),
        permissions=permissions({"id": "all", "decision": "deny"}),
    )

    assert "permission.decided" not in types_of(records)
    assert "Unknown tool: missing" in tool_messages(result)[0]
